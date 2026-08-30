"""MongoDB access for decision metadata.

One document per decision, keyed by `<body_slug>:<identifier_slug>`. That key is
deterministic, which is what makes the pipeline idempotent structurally rather
than by convention: re-scraping upserts the same document, so duplicates are
impossible even when a run is interrupted and repeated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from kedra_scraper.config import MongoSettings

LANDING_COLLECTION = "decisions"
CURATED_COLLECTION = "curated_decisions"
PARTITION_RUNS_COLLECTION = "partition_runs"


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    Explicitly aware, not `utcnow()`: naive datetimes round-trip through BSON as
    UTC but compare incorrectly against aware ones in application code, which
    surfaces much later as a confusing timezone bug.
    """
    return datetime.now(UTC)


class CuratedRepository:
    """The curated collection: one document per transformed decision.

    Separate from the landing collection rather than extra fields on it. The
    landing record describes what the site served and must not change when the
    cleaning rules do; the curated record is derived and is expected to be
    rebuilt whenever those rules improve. Mixing them would make it impossible
    to re-run the transformation without rewriting the record of what was
    originally scraped.
    """

    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._db = database

    @classmethod
    def from_settings(cls, settings: MongoSettings) -> CuratedRepository:
        client: MongoClient[dict[str, Any]] = MongoClient(settings.uri)
        return cls(client[settings.db])

    @property
    def collection(self) -> Collection[dict[str, Any]]:
        return self._db[CURATED_COLLECTION]

    def ensure_indexes(self) -> None:
        self.collection.create_index([("body", ASCENDING), ("decision_date", DESCENDING)])
        self.collection.create_index([("partition_date", ASCENDING)])
        # Quality flags are the reason this collection is worth querying: "show
        # me everything that came out stubbed or damaged" should not be a scan.
        self.collection.create_index([("quality_flags", ASCENDING)])

    def get(self, document_id: str) -> dict[str, Any] | None:
        return self.collection.find_one({"_id": document_id})

    def upsert(self, document: dict[str, Any]) -> bool:
        document_id = document["_id"]
        payload = {k: v for k, v in document.items() if k != "_id"}
        result = self.collection.update_one(
            {"_id": document_id},
            {"$set": payload, "$setOnInsert": {"first_curated_at": utcnow()}},
            upsert=True,
        )
        return result.upserted_id is not None


class PartitionRunRepository:
    """One record per crawl of one body-month: what it found and what it stored.

    This exists so the reconciliation outcome outlives the process that computed
    it. The spider knows whether a partition reconciled, but that knowledge is
    otherwise trapped in a log line and a subprocess exit code. Persisting it
    means an asset check can assert on it later, and a reviewer can ask "which
    partitions have ever failed to reconcile?" without grepping logs.
    """

    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._db = database

    @classmethod
    def from_settings(cls, settings: MongoSettings) -> PartitionRunRepository:
        client: MongoClient[dict[str, Any]] = MongoClient(settings.uri)
        return cls(client[settings.db])

    @property
    def collection(self) -> Collection[dict[str, Any]]:
        return self._db[PARTITION_RUNS_COLLECTION]

    def record(self, stats: dict[str, Any]) -> None:
        """Store the outcome of one partition crawl, keyed so re-runs overwrite.

        Keyed on body plus partition rather than appended, because the question
        being asked is "is this partition currently healthy?", not "how many
        times has it been attempted?". A re-run that now reconciles should
        replace a previous failure, not sit behind it in a history.
        """
        key = f"{stats['body']}:{stats['partition_date']}"
        self.collection.update_one(
            {"_id": key}, {"$set": {**stats, "recorded_at": utcnow()}}, upsert=True
        )

    def latest(self, body: str, partition_date: str) -> dict[str, Any] | None:
        return self.collection.find_one({"_id": f"{body}:{partition_date}"})


class DecisionRepository:
    """Reads and writes for the landing-zone metadata collection."""

    def __init__(self, database: Database[dict[str, Any]]) -> None:
        self._db = database

    @classmethod
    def from_settings(cls, settings: MongoSettings) -> DecisionRepository:
        client: MongoClient[dict[str, Any]] = MongoClient(settings.uri)
        return cls(client[settings.db])

    @property
    def collection(self) -> Collection[dict[str, Any]]:
        return self._db[LANDING_COLLECTION]

    def ensure_indexes(self) -> None:
        """Create the indexes the pipeline depends on. Safe to call repeatedly.

        Called at crawl start rather than left to a migration step, because a
        missing index here degrades silently: the dedup lookup still returns
        correct answers, just with a collection scan per document, so the crawl
        gets slower and slower as the corpus grows with nothing to show why.
        """
        self.collection.create_index([("body", ASCENDING), ("decision_date", DESCENDING)])
        self.collection.create_index([("partition_date", ASCENDING)])
        # Change detection looks documents up by this, and dedup across bodies
        # scans it, so it earns an index despite not being unique.
        self.collection.create_index([("content_hash", ASCENDING)])
        self.collection.create_index([("status", ASCENDING)])

    def get(self, document_id: str) -> dict[str, Any] | None:
        """Fetch one record, or None. Used to decide whether anything changed."""
        return self.collection.find_one({"_id": document_id})

    def upsert(self, document: dict[str, Any]) -> bool:
        """Insert or update one decision. Returns True if this was a new record.

        `first_seen_at` is set only on insert via `$setOnInsert`, so it records
        when the document entered the corpus and is not overwritten by later
        runs. Everything else is refreshed on every pass.
        """
        document_id = document["_id"]
        payload = {k: v for k, v in document.items() if k != "_id"}
        result = self.collection.update_one(
            {"_id": document_id},
            {"$set": payload, "$setOnInsert": {"first_seen_at": utcnow()}},
            upsert=True,
        )
        return result.upserted_id is not None

    def touch(self, document_id: str) -> None:
        """Record that an unchanged document was seen again.

        A skipped document is still evidence the source still publishes it, and
        that is worth keeping: `last_seen_at` is how a later run can distinguish
        "unchanged" from "withdrawn from the site".
        """
        self.collection.update_one({"_id": document_id}, {"$set": {"last_seen_at": utcnow()}})

    def count_for_partition(self, body: str, partition_date: str) -> int:
        """How many records exist for one partition.

        This is the scraped side of the found-vs-scraped reconciliation.
        """
        return self.collection.count_documents(
            {"body": body, "partition_date": partition_date, "status": "ok"}
        )
