"""Persistence: bytes to object storage, then metadata to Mongo.

The order is load-bearing, and it is why these are two pipelines rather than
one. Blobs are written first so that a metadata record never points at an object
that does not exist. Crashing between the two leaves an orphaned object --
wasted bytes, harmless, and re-runnable. The reverse order would leave a
dangling pointer: a record that looks scraped, satisfies the count
reconciliation, and fails only when something later tries to read the document.

Scrapy runs pipelines in ascending order of their configured number, so
ObjectStoragePipeline (100) always precedes MongoMetadataPipeline (200).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from scrapy import Spider

from kedra_scraper.config import get_settings
from kedra_scraper.hashing import content_hash, file_hash
from kedra_scraper.logging_setup import get_logger
from kedra_scraper.naming import document_id, landing_key
from kedra_scraper.scraper.items import DecisionItem
from kedra_scraper.storage.mongo import DecisionRepository, utcnow
from kedra_scraper.storage.objects import ObjectStore

log = get_logger(__name__)


class ObjectStoragePipeline:
    """Write every retrieved file to the landing bucket, then drop the payload."""

    def __init__(self) -> None:
        settings = get_settings()
        self._store = ObjectStore.from_settings(settings.s3)
        self._bucket = settings.landing_bucket
        self.objects_written = 0
        self.objects_already_present = 0

    def process_item(self, item: DecisionItem, spider: Spider) -> DecisionItem:
        item.document_id = document_id(item.body, item.identifier)

        for stored in item.files:
            if stored.skipped_reason or not stored.data:
                continue

            is_html = stored.content_type.startswith("text/html")
            stored.size = len(stored.data)
            stored.file_hash = file_hash(stored.data)
            stored.content_hash = content_hash(stored.data, is_html=is_html)

            key = landing_key(item.body, item.partition_date, item.identifier, stored.filename)
            written = self._store.put_if_absent(
                self._bucket, key, stored.data, content_type=stored.content_type
            )
            if written:
                self.objects_written += 1
            else:
                self.objects_already_present += 1

            # A URI rather than a bare key: the record should say which store it
            # refers to, so it stays meaningful if a second bucket is ever added.
            stored.path = f"s3://{self._bucket}/{key}"

            # Payload dropped once persisted. Without this the bytes travel on
            # into the Mongo pipeline and a 800KB page would be written into the
            # metadata document itself.
            stored.data = b""

        self._promote_primary(item)
        return item

    @staticmethod
    def _promote_primary(item: DecisionItem) -> None:
        """Hoist the authoritative file's details onto the record itself.

        The brief asks for the file's path and hash in the metadata, so they are
        top-level fields rather than something a consumer has to dig out of the
        `files` list.

        When the primary could not be retrieved but something else was -- an
        Equality Tribunal record whose PDF is robots-disallowed but whose HTML
        abstract is not -- the record is marked `partial` and points at what was
        actually stored. It is neither silently complete nor thrown away.
        """
        stored_files = [f for f in item.files if f.path]
        primary = next((f for f in stored_files if f.kind == "primary"), None)
        chosen = primary or (stored_files[0] if stored_files else None)

        if chosen is None:
            item.status = "failed"
            item.error = "; ".join(f.skipped_reason for f in item.files if f.skipped_reason)
            return

        item.file_path = chosen.path
        item.file_hash = chosen.file_hash
        item.content_hash = chosen.content_hash
        item.file_size = chosen.size
        item.content_type = chosen.content_type
        item.status = "ok" if primary is not None else "partial"
        if primary is None:
            item.error = "; ".join(f.skipped_reason for f in item.files if f.skipped_reason)


class MongoMetadataPipeline:
    """Upsert one metadata document per decision."""

    def __init__(self) -> None:
        settings = get_settings()
        self._repo = DecisionRepository.from_settings(settings.mongo)
        self.inserted = 0
        self.updated = 0

    def open_spider(self, spider: Spider) -> None:
        # Idempotent, and cheap. Doing it here rather than in a migration means
        # a fresh clone works after `make up` with no extra step.
        self._repo.ensure_indexes()

    def process_item(self, item: DecisionItem, spider: Spider) -> DecisionItem:
        document = self._to_document(item)
        if self._repo.upsert(document):
            self.inserted += 1
        else:
            self.updated += 1
        return item

    @staticmethod
    def _to_document(item: DecisionItem) -> dict[str, Any]:
        """Flatten the item into the stored metadata shape.

        Dates become datetimes because BSON has no date type; storing them as
        strings would make range queries lexical rather than chronological.
        """

        def as_dt(value: Any) -> datetime:
            return datetime(value.year, value.month, value.day)

        files = []
        for stored in item.files:
            entry = asdict(stored)
            entry.pop("data", None)  # never persist payloads
            files.append(entry)

        return {
            "_id": item.document_id,
            "identifier": item.identifier,
            "body": item.body,
            "body_id": item.body_id,
            "title": item.title,
            "description": item.description,
            "decision_date": as_dt(item.decision_date),
            "partition_date": item.partition_date,
            "partition_start": as_dt(item.partition_start),
            "partition_end": as_dt(item.partition_end),
            "source_url": item.source_url,
            "doc_type": item.doc_type,
            "file_path": item.file_path,
            "file_hash": item.file_hash,
            "content_hash": item.content_hash,
            "file_size": item.file_size,
            "content_type": item.content_type,
            "files": files,
            "status": item.status,
            "error": item.error,
            "run_id": item.run_id,
            "scraper_version": item.scraper_version,
            "last_seen_at": utcnow(),
        }
