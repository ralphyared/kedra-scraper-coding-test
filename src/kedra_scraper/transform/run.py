"""Driving the landing zone into the curated zone.

Reads metadata from Mongo, pulls each stored object, derives a curated form, and
writes it to a second bucket and collection. The landing zone is only ever read.

Two document classes are handled differently, and the difference is the point:

* HTML is cleaned -- page furniture removed, a few fields extracted.
* PDFs and Word documents are copied byte-for-byte.

There is no meaningful "cleaning" of a PDF short of re-rendering it, and
re-rendering would produce a document that is no longer the one the tribunal
issued. For a legal corpus that is the wrong trade: the curated copy is renamed
and re-indexed, never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from kedra_scraper.config import get_settings
from kedra_scraper.hashing import content_hash, file_hash
from kedra_scraper.logging_setup import get_logger, new_run_id
from kedra_scraper.naming import curated_key
from kedra_scraper.storage.mongo import CuratedRepository, DecisionRepository, utcnow
from kedra_scraper.storage.objects import ObjectStore, content_type_for
from kedra_scraper.transform.cleaner import clean_html
from kedra_scraper.transform.enrich import enrich

log = get_logger(__name__)

_HTML_TYPES = ("text/html",)


@dataclass
class TransformStats:
    """What one transformation pass did, for reconciliation against landing."""

    considered: int = 0
    transformed: int = 0
    unchanged: int = 0
    failed: int = 0
    flagged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def accounted(self) -> int:
        return self.transformed + self.unchanged + self.failed

    @property
    def reconciled(self) -> bool:
        return self.accounted == self.considered

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "transformed": self.transformed,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "flagged": self.flagged,
            "reconciled": self.reconciled,
        }


def _split_uri(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def _extension(filename: str, content_type: str) -> str:
    """The extension the curated copy should carry.

    Taken from the stored filename where it has one, because that is what the
    site actually served. The content type is only a fallback: EAT attachments
    are named with bare UUIDs on some pages.
    """
    _, dot, extension = filename.rpartition(".")
    if dot and len(extension) <= 5:
        return extension.lower()
    return "html" if content_type.startswith(_HTML_TYPES) else "bin"


def transform_range(
    start: date,
    end: date,
    body: str | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> TransformStats:
    """Transform every landing record whose decision date falls in the range.

    Idempotent by the same mechanism as the crawl: a curated record already
    derived from the same source `content_hash` is left alone. Re-running after
    changing the cleaning rules therefore needs `force`, which is the honest
    behaviour -- the input has not changed, the code has.
    """
    settings = get_settings()
    run_id = run_id or new_run_id()

    landing = DecisionRepository.from_settings(settings.mongo)
    curated = CuratedRepository.from_settings(settings.mongo)
    store = ObjectStore.from_settings(settings.s3)
    curated.ensure_indexes()

    query: dict[str, Any] = {
        "decision_date": {
            "$gte": datetime(start.year, start.month, start.day),
            "$lte": datetime(end.year, end.month, end.day),
        },
        # A record with no stored object has nothing to transform. These are
        # already counted as failures by the crawl, and counting them again here
        # would double-report the same incident.
        "file_path": {"$ne": ""},
    }
    if body:
        query["body"] = body

    stats = TransformStats()
    log.info(
        "transform_started",
        run_id=run_id,
        body=body or "all",
        date_from=start.isoformat(),
        date_to=end.isoformat(),
    )

    for record in landing.collection.find(query):
        stats.considered += 1
        try:
            if _transform_one(record, store, curated, settings, run_id, force=force):
                stats.transformed += 1
                if record.get("_curated_flags"):
                    stats.flagged += 1
            else:
                stats.unchanged += 1
        except Exception as exc:
            stats.failed += 1
            stats.errors.append(f"{record['_id']}: {exc}")
            log.error("transform_failed", document_id=record["_id"], error=str(exc))

    log.info("transform_finished", run_id=run_id, **stats.as_dict())
    return stats


def _transform_one(
    record: dict[str, Any],
    store: ObjectStore,
    curated: CuratedRepository,
    settings: Any,
    run_id: str,
    *,
    force: bool,
) -> bool:
    """Derive one curated document. Returns True if it was (re)written."""
    document_id = record["_id"]
    existing = curated.get(document_id)
    if not force and existing and existing.get("source_content_hash") == record["content_hash"]:
        return False

    bucket, key = _split_uri(record["file_path"])
    raw = store.get_bytes(bucket, key)

    source_type = record.get("content_type", "")
    is_html = source_type.startswith(_HTML_TYPES)

    quality_flags: tuple[str, ...] = ()
    enrichment: dict[str, Any] = {}
    text = ""

    if is_html:
        cleaned = clean_html(raw.decode("utf-8", errors="replace"))
        extracted = enrich(cleaned.html, cleaned.text)
        payload = cleaned.html.encode("utf-8")
        text = cleaned.text
        quality_flags = extracted.quality_flags
        enrichment = {
            "hearing_date": (
                datetime.combine(extracted.hearing_date, datetime.min.time())
                if extracted.hearing_date
                else None
            ),
            "adjudicator": extracted.adjudicator,
            "legislation": extracted.legislation,
            "internal_reference": extracted.internal_reference,
            "complaint_references": list(extracted.complaint_references),
            "parties": list(extracted.parties),
            "representatives": list(extracted.representatives),
            "title": cleaned.title,
            "word_count": cleaned.word_count,
            "char_count": cleaned.char_count,
        }
    else:
        # Copied verbatim. Re-rendering a PDF would produce a document that is
        # no longer the one the tribunal issued.
        payload = raw

    filename = record["file_path"].rsplit("/", 1)[-1]
    extension = _extension(filename, source_type)
    key_out = curated_key(record["body"], record["partition_date"], record["identifier"], extension)
    store.put(
        settings.curated_bucket,
        key_out,
        payload,
        content_type=content_type_for(f"x.{extension}"),
    )

    curated.upsert(
        {
            "_id": document_id,
            "identifier": record["identifier"],
            "body": record["body"],
            "decision_date": record["decision_date"],
            "partition_date": record["partition_date"],
            "source_url": record["source_url"],
            "doc_type": record["doc_type"],
            # Which landing bytes this was derived from. Comparing it on the
            # next pass is what makes the transformation idempotent, and it also
            # says exactly which version of the source a curated copy reflects.
            "source_path": record["file_path"],
            "source_content_hash": record["content_hash"],
            "curated_path": f"s3://{settings.curated_bucket}/{key_out}",
            "curated_file_hash": file_hash(payload),
            "curated_content_hash": content_hash(payload, is_html=is_html),
            "curated_size": len(payload),
            "content_type": content_type_for(f"x.{extension}"),
            "text": text,
            "quality_flags": list(quality_flags),
            # How the document was handled, not whether anything is wrong with
            # it. Kept separate from quality_flags so that filtering on flags
            # returns problems rather than every PDF in the corpus.
            "passthrough": not is_html,
            "transformed_at": utcnow(),
            "run_id": run_id,
            **enrichment,
        }
    )
    record["_curated_flags"] = quality_flags
    return True
