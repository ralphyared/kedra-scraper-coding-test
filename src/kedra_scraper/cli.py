"""Command-line entry point.

`crawl` runs one Scrapy process per partition rather than crawling a whole date
range in a single process. That mirrors exactly what the Dagster asset will do
in Phase 5, and for the same reason: Twisted's reactor cannot be restarted
within a process, so a second in-process crawl raises `ReactorNotRestartable`.
Running each partition as a subprocess also isolates crashes, so one bad month
cannot take down a backfill.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import typer

from kedra_scraper.bodies import BODIES, resolve
from kedra_scraper.config import get_settings
from kedra_scraper.hashing import file_hash
from kedra_scraper.logging_setup import configure_logging, get_logger, new_run_id
from kedra_scraper.partitions import Granularity, iter_partitions
from kedra_scraper.storage.mongo import DecisionRepository
from kedra_scraper.storage.objects import ObjectStore
from kedra_scraper.transform.run import transform_range

app = typer.Typer(add_completion=False, help="Scrape WRC decisions into a landing zone.")
log = get_logger(__name__)


def _bodies_for(selector: str) -> list[str]:
    """Expand `all` into every body, or resolve a single name."""
    if selector.lower() == "all":
        return [body.slug for body in BODIES]
    return [resolve(selector).slug]


@app.command()
def crawl(
    start: str = typer.Option(..., help="Inclusive start date, YYYY-MM-DD."),
    end: str = typer.Option(..., help="Inclusive end date, YYYY-MM-DD."),
    body: str = typer.Option("all", help="Body slug or alias, or 'all'."),
    granularity: str = typer.Option("", help="Override PARTITION_GRANULARITY."),
) -> None:
    """Crawl a date range, one subprocess per body-partition."""
    settings = get_settings()
    run_id = new_run_id()
    configure_logging(level=settings.log_level, log_dir=settings.log_dir, run_id=run_id)

    grain: Granularity = granularity or settings.partition_granularity  # type: ignore[assignment]
    partitions = iter_partitions(date.fromisoformat(start), date.fromisoformat(end), grain)
    bodies = _bodies_for(body)

    log.info(
        "run_started",
        run_id=run_id,
        bodies=bodies,
        partitions=[p.label for p in partitions],
        units=len(bodies) * len(partitions),
    )

    failures = 0
    # Requirement 10 asks for a summary at the end of each run, not only per
    # partition. Each subprocess writes its counters to a stats file and they
    # are totalled here, so a twelve-partition run answers "how many records
    # did this find, and how many did it store?" in one place rather than
    # leaving a reader to add up twelve log lines.
    totals: dict[str, int] = defaultdict(int)
    per_partition: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp:
        for body_slug in bodies:
            for partition in partitions:
                stats_path = Path(tmp) / f"{body_slug}-{partition.label}.json"
                command = [
                    sys.executable, "-m", "scrapy", "crawl", "wrc",
                    "-a", f"body={body_slug}",
                    "-a", f"start={partition.start.isoformat()}",
                    "-a", f"end={partition.end.isoformat()}",
                    "-a", f"partition={partition.label}",
                    "-a", f"run_id={run_id}",
                    "-a", f"stats_file={stats_path}",
                ]  # fmt: skip
                result = subprocess.run(command, check=False)

                if result.returncode != 0:
                    failures += 1
                    log.error(
                        "partition_subprocess_failed",
                        body=body_slug,
                        partition_date=partition.label,
                        exit_code=result.returncode,
                    )
                    continue

                if not stats_path.exists():
                    # Exited cleanly but reported nothing. Counted as a failure
                    # rather than ignored: a partition whose outcome is unknown
                    # must not be silently absent from the totals.
                    failures += 1
                    log.error(
                        "partition_stats_missing",
                        body=body_slug,
                        partition_date=partition.label,
                    )
                    continue

                stats = json.loads(stats_path.read_text(encoding="utf-8"))
                per_partition.append(stats)
                for key in (
                    "total_reported", "items_emitted", "skipped_unchanged",
                    "duplicate_rows", "failures", "unique_documents",
                    "files_skipped", "conditional_hits", "unaccounted",
                ):  # fmt: skip
                    totals[key] += int(stats.get(key, 0))

    log.info(
        "run_summary",
        run_id=run_id,
        bodies=bodies,
        partitions_attempted=len(bodies) * len(partitions),
        partitions_completed=len(per_partition),
        partitions_failed=failures,
        # Every partition reconciled individually, so the run did too.
        all_reconciled=all(p.get("reconciled") for p in per_partition) and not failures,
        **dict(totals),
    )
    if failures:
        raise typer.Exit(code=1)


@app.command()
def transform(
    start: str = typer.Option(..., help="Inclusive start date, YYYY-MM-DD."),
    end: str = typer.Option(..., help="Inclusive end date, YYYY-MM-DD."),
    body: str = typer.Option("", help="Restrict to one body; default is all."),
    force: bool = typer.Option(False, help="Re-derive even where the source has not changed."),
) -> None:
    """Derive the curated zone from the landing zone for a date range.

    `--force` exists because the transformation is idempotent against its
    *input*, not its code: an unchanged source is skipped even if the cleaning
    rules have since improved. Rebuilding after a rules change is therefore an
    explicit act rather than something that happens silently on the next run.
    """
    settings = get_settings()
    run_id = new_run_id()
    configure_logging(level=settings.log_level, log_dir=settings.log_dir, run_id=run_id)

    stats = transform_range(
        date.fromisoformat(start),
        date.fromisoformat(end),
        body=resolve(body).slug if body else None,
        run_id=run_id,
        force=force,
    )
    typer.echo(
        f"considered={stats.considered} transformed={stats.transformed} "
        f"unchanged={stats.unchanged} failed={stats.failed} flagged={stats.flagged}"
    )
    for error in stats.errors[:10]:
        typer.echo(f"  {error}")
    if not stats.reconciled or stats.failed:
        raise typer.Exit(code=1)


@app.command()
def stats() -> None:
    """Summarise what is currently in the landing zone."""
    repo = DecisionRepository.from_settings(get_settings().mongo)
    rows = repo.collection.aggregate(
        [
            {
                "$group": {
                    "_id": {"body": "$body", "partition": "$partition_date", "status": "$status"},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id.body": 1, "_id.partition": 1}},
        ]
    )
    typer.echo(f"{'body':<30} {'partition':<12} {'status':<9} count")
    total = 0
    for row in rows:
        key = row["_id"]
        total += row["count"]
        typer.echo(f"{key['body']:<30} {key['partition']:<12} {key['status']:<9} {row['count']}")
    typer.echo(f"{'':<30} {'':<12} {'TOTAL':<9} {total}")


@app.command()
def verify(limit: int = typer.Option(0, help="Check only the first N records (0 = all).")) -> None:
    """Check that every metadata record points at an object that really exists.

    This is the guarantee the pipeline ordering is designed to provide -- blobs
    are written before metadata, so a dangling pointer should be impossible.
    Asserting it is cheap, and a violation would mean the ordering has been
    broken by a later change.
    """
    settings = get_settings()
    repo = DecisionRepository.from_settings(settings.mongo)
    store = ObjectStore.from_settings(settings.s3)

    cursor = repo.collection.find({"file_path": {"$ne": ""}})
    if limit:
        cursor = cursor.limit(limit)

    checked = missing = corrupt = 0
    for doc in cursor:
        checked += 1
        bucket, _, key = doc["file_path"].removeprefix("s3://").partition("/")
        if not store.exists(bucket, key):
            missing += 1
            typer.echo(f"MISSING  {doc['_id']}  {doc['file_path']}")
            continue
        # Recomputing the hash also proves the bytes were not altered in place,
        # which is the landing zone's other invariant.
        if file_hash(store.get_bytes(bucket, key)) != doc["file_hash"]:
            corrupt += 1
            typer.echo(f"HASH MISMATCH  {doc['_id']}")

    typer.echo(f"\nchecked={checked} missing={missing} hash_mismatch={corrupt}")
    if missing or corrupt:
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
