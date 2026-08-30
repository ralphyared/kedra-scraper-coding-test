"""The landing-zone asset and the check that keeps it honest.

One materialisation per body-month. The asset does not crawl in-process: it runs
the existing spider as a subprocess and reads back a stats file.

Two reasons, and the first is not stylistic. Twisted's reactor cannot be
restarted within a process, so a second in-process crawl raises
`ReactorNotRestartable` -- an in-process `CrawlerProcess` would materialise the
first partition and then fail every subsequent one in the same worker. The
subprocess boundary also isolates crashes, so one malformed month cannot take
down a twelve-partition backfill, and it keeps the spider runnable on its own.

Note the absence of `from __future__ import annotations` here, which is
deliberate. Under PEP 563 annotations become strings, and Dagster validates the
`context` parameter by comparing the annotation against the actual class at
decoration time -- so the future import makes every asset in the module fail to
load with a confusing "cannot annotate context parameter" error.
"""

import calendar
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
    asset_check,
)

from kedra_scraper.config import get_settings
from kedra_scraper.storage.mongo import DecisionRepository, PartitionRunRepository
from kedra_scraper.transform.run import transform_range

from .partitions import BODY_DIMENSION, MONTH_DIMENSION, decision_partitions


def month_bounds(month_key: str) -> tuple[date, date]:
    """Turn a `YYYY-MM-DD` partition key into that month's inclusive bounds.

    Dagster names a monthly partition by its first day. The spider takes an
    inclusive range, so the last day is computed rather than assumed -- a
    hardcoded 28 or 30 would silently drop decisions published on the days it
    omitted, once every four years for February.
    """
    start = date.fromisoformat(month_key)
    return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])


def partition_label(start: date) -> str:
    """The label the rest of the system stores, e.g. `2024-01`."""
    return f"{start.year:04d}-{start.month:02d}"


@asset(
    partitions_def=decision_partitions,
    group_name="landing",
    description="Raw decisions and metadata for one body-month, exactly as served.",
)
def landing_documents(context: AssetExecutionContext) -> MaterializeResult[None]:
    """Crawl one body-month into the landing zone."""
    keys = context.partition_key.keys_by_dimension
    body = keys[BODY_DIMENSION]
    start, end = month_bounds(keys[MONTH_DIMENSION])
    label = partition_label(start)

    with tempfile.TemporaryDirectory() as tmp:
        stats_path = Path(tmp) / "stats.json"
        command = [
            sys.executable, "-m", "scrapy", "crawl", "wrc",
            "-a", f"body={body}",
            "-a", f"start={start.isoformat()}",
            "-a", f"end={end.isoformat()}",
            "-a", f"partition={label}",
            "-a", f"run_id={context.run_id}",
            "-a", f"stats_file={stats_path}",
        ]  # fmt: skip
        context.log.info("crawling %s %s", body, label)
        result = subprocess.run(command, check=False, capture_output=True, text=True)

        if result.returncode != 0:
            # The spider's own stderr is the useful part; surfacing it here means
            # a reviewer reads the failure in the Dagster UI rather than hunting
            # through a subprocess log.
            context.log.error(result.stderr[-4000:])
            raise RuntimeError(f"crawl of {body} {label} exited {result.returncode}")

        if not stats_path.exists():
            raise RuntimeError(f"crawl of {body} {label} exited 0 but wrote no stats file")
        stats: dict[str, Any] = json.loads(stats_path.read_text(encoding="utf-8"))

    # Persisted so the outcome outlives this process and the asset check can
    # assert on it without re-deriving arithmetic the spider already did.
    PartitionRunRepository.from_settings(get_settings().mongo).record(stats)

    return MaterializeResult(
        metadata={
            # Surfaced on the materialisation so the grid is readable without
            # opening logs: what the site claimed, and where every row went.
            "total_reported": stats["total_reported"],
            "items_emitted": stats["items_emitted"],
            "skipped_unchanged": stats["skipped_unchanged"],
            "duplicate_rows": stats["duplicate_rows"],
            "failures": stats["failures"],
            "unique_documents": stats["unique_documents"],
            "files_skipped": stats["files_skipped"],
            "conditional_hits": stats["conditional_hits"],
            "unaccounted": stats["unaccounted"],
            "reconciled": stats["reconciled"],
            "search_url": MetadataValue.url(
                "https://www.workplacerelations.ie/en/search/?decisions=1"
                f"&from={start.strftime('%d/%m/%Y')}&to={end.strftime('%d/%m/%Y')}"
            ),
        }
    )


@asset(
    partitions_def=decision_partitions,
    deps=[landing_documents],
    group_name="curated",
    description="Cleaned, renamed and lightly enriched documents for one body-month.",
)
def curated_documents(context: AssetExecutionContext) -> MaterializeResult[None]:
    """Derive the curated zone for one body-month from the landing zone.

    `deps=[landing_documents]` rather than a value dependency: nothing is passed
    between the two assets in memory. The landing asset's output is rows in
    Mongo and objects in a bucket, and this reads them from there. Declaring the
    edge still buys what matters -- Dagster will not curate a partition before
    it has been crawled, and re-crawling a month marks its curated partition
    stale in the UI.

    Runs in-process, unlike the crawl. There is no Twisted reactor involved
    here, so the subprocess isolation the landing asset needs would be pure
    overhead.
    """
    keys = context.partition_key.keys_by_dimension
    body = keys[BODY_DIMENSION]
    start, end = month_bounds(keys[MONTH_DIMENSION])

    stats = transform_range(start, end, body=body, run_id=context.run_id)
    context.log.info(
        "curated %s %s: %d transformed, %d unchanged, %d failed",
        body,
        partition_label(start),
        stats.transformed,
        stats.unchanged,
        stats.failed,
    )

    if stats.failed:
        # A failure here means a landing object could not be read or parsed.
        # Unlike an incomplete crawl, there is nothing partial worth keeping --
        # the curated zone is derived and can simply be rebuilt -- so this fails
        # the materialisation rather than recording a degraded result.
        raise RuntimeError(f"{stats.failed} document(s) failed to transform: {stats.errors[:3]}")

    return MaterializeResult(
        metadata={
            "considered": stats.considered,
            "transformed": stats.transformed,
            "unchanged": stats.unchanged,
            "flagged": stats.flagged,
            "reconciled": stats.reconciled,
        }
    )


@asset_check(
    asset=landing_documents,
    description="Every listing row the site reported is accounted for.",
)
def every_row_is_accounted_for(context: AssetCheckExecutionContext) -> AssetCheckResult:
    """Assert the brief's accounting rule mechanically.

    The rule is: scrape everything the source reported, or scrape less and
    explain each miss. The spider already reduces that to a single boolean, and
    this reads it rather than recomputing it -- two implementations of the same
    arithmetic drift apart, and the one shown in the UI would be the one nobody
    trusts.

    A check rather than an exception inside the asset, deliberately. A partition
    that scraped 230 of 234 has still produced real data worth keeping; raising
    would discard it. The check marks the result untrustworthy while leaving it
    available, which is the distinction between "this data is wrong" and "this
    data is incomplete and we know by how much".
    """
    keys = context.partition_key.keys_by_dimension
    body = keys[BODY_DIMENSION]
    start, _ = month_bounds(keys[MONTH_DIMENSION])
    label = partition_label(start)

    settings = get_settings()
    run = PartitionRunRepository.from_settings(settings.mongo).latest(body, label)
    if run is None:
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.ERROR,
            metadata={"error": "no crawl has been recorded for this partition"},
        )

    stored = DecisionRepository.from_settings(settings.mongo).collection.count_documents(
        {"body": body, "partition_date": label}
    )

    # Two independent conditions. The first says the crawl explained itself; the
    # second says the database actually holds what the crawl claimed. A crawl
    # can reconcile perfectly and still have failed to persist, which is exactly
    # the failure the blob-before-metadata ordering is designed to make loud.
    return AssetCheckResult(
        passed=bool(run["reconciled"]) and stored >= int(run["unique_documents"]),
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "total_reported": run["total_reported"],
            "unaccounted_rows": run["unaccounted"],
            "unique_documents_reported": run["unique_documents"],
            "records_in_mongo": stored,
        },
    )
