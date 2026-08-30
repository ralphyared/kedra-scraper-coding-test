"""Tests for the machinery that makes a second run cheap and safe.

Requirement 9 asks for idempotent re-runs. Three mechanisms deliver it, and
each is tested here for the property that would silently break it:

* deterministic keys, so a re-run upserts rather than duplicating
* content hashing, so an unchanged document is recognised as unchanged
* append-only writes, so a *changed* document never overwrites its predecessor
"""

from __future__ import annotations

from datetime import date

from kedra_scraper.naming import landing_key, versioned_filename
from kedra_scraper.scraper.items import DecisionItem, StoredFile
from kedra_scraper.scraper.pipelines import ObjectStoragePipeline
from kedra_scraper.scraper.spiders.wrc import classify_failure, failure_status


class FakeStore:
    """Stands in for the object store, recording what a run would write."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        # key -> content hash already stored there
        self.existing = dict(existing or {})
        self.writes: list[tuple[str, str]] = []

    def stored_content_hash(self, bucket: str, key: str) -> str | None:
        return self.existing.get(key)

    def exists(self, bucket: str, key: str) -> bool:
        return key in self.existing

    def put_if_absent(
        self, bucket: str, key: str, data: bytes, *, content_type: str = "", content_hash: str = ""
    ) -> bool:
        if key in self.existing:
            return False
        self.existing[key] = content_hash
        self.writes.append((key, content_hash))
        return True


def _pipeline(store: FakeStore) -> ObjectStoragePipeline:
    return ObjectStoragePipeline(store=store, bucket="landing")  # type: ignore[arg-type]


def _item() -> DecisionItem:
    return DecisionItem(
        identifier="ADJ-00046159",
        body="workplace_relations_commission",
        body_id=15376,
        title="t",
        description="",
        decision_date=date(2024, 1, 31),
        partition_date="2024-01",
        partition_start=date(2024, 1, 1),
        partition_end=date(2024, 1, 31),
        source_url="https://example.invalid/x.html",
        doc_type="html",
        run_id="r",
        scraper_version="0.1.0",
    )


def _file(content_hash: str) -> StoredFile:
    return StoredFile(
        kind="primary",
        url="https://example.invalid/x.html",
        filename="adj-00046159.html",
        content_hash=content_hash,
        content_type="text/html; charset=utf-8",
    )


BASE_KEY = "body=workplace_relations_commission/partition=2024-01/adj-00046159/adj-00046159.html"


# --------------------------------------------------------------------------
# Append-only key selection
# --------------------------------------------------------------------------


def test_a_first_time_document_uses_the_clean_name() -> None:
    pipeline = _pipeline(FakeStore())
    assert pipeline._key_for(_item(), _file("sha256:aaaa")) == BASE_KEY


def test_unchanged_content_resolves_to_the_same_key() -> None:
    """This is what makes a re-run free.

    Same content means same key, and `put_if_absent` then skips the upload. If
    the key changed on every run instead, every re-run would duplicate the whole
    corpus while appearing to work.
    """
    store = FakeStore({BASE_KEY: "sha256:aaaa"})
    pipeline = _pipeline(store)
    assert pipeline._key_for(_item(), _file("sha256:aaaa")) == BASE_KEY
    assert store.writes == []


def test_changed_content_goes_to_a_new_versioned_key() -> None:
    """The brief forbids updating landing data, so a change cannot overwrite.

    The predecessor stays exactly where it was; the new bytes go alongside it
    under a hash-suffixed name.
    """
    store = FakeStore({BASE_KEY: "sha256:oldhash"})
    key = _pipeline(store)._key_for(_item(), _file("sha256:8807fd270868ccb7"))
    assert key != BASE_KEY
    assert key.endswith("adj-00046159__8807fd270868.html")


def test_the_previous_version_is_never_touched() -> None:
    """End to end through the pipeline, not just the key chooser.

    Asserts both halves of the append-only guarantee: the earlier object still
    holds its original content hash, and the new bytes were written to a second
    key rather than replacing it.
    """
    store = FakeStore({BASE_KEY: "sha256:oldhash"})
    stored = _file("")  # hash is recomputed from the payload by the pipeline
    stored.data = b"<html>a genuinely different document</html>"
    item = _item()
    item.files.append(stored)

    _pipeline(store).process_item(item, spider=None)  # type: ignore[arg-type]

    assert store.existing[BASE_KEY] == "sha256:oldhash", "the earlier version was overwritten"
    assert len(store.writes) == 1, "the changed document was not written"
    new_key, _ = store.writes[0]
    assert new_key != BASE_KEY
    assert "__" in new_key.rsplit("/", 1)[-1]
    # The record must point at the version just written, not the older one.
    assert item.file_path == f"s3://landing/{new_key}"


def test_version_marker_sits_before_the_extension() -> None:
    """So the file still reads as HTML or PDF to anything dispatching on suffix."""
    assert versioned_filename("a.pdf", "abc123") == "a__abc123.pdf"
    assert versioned_filename("adj-1.html", "deadbeef") == "adj-1__deadbeef.html"


def test_a_filename_without_an_extension_still_versions_cleanly() -> None:
    assert versioned_filename("noext", "abc123") == "noext__abc123"


def test_landing_key_without_a_version_is_unchanged() -> None:
    """Existing objects must keep resolving to the key they were written under."""
    assert landing_key("b", "2024-01", "ADJ-1", "f.html") == "body=b/partition=2024-01/adj-1/f.html"


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


class FakeFailure:
    """Minimal stand-in for a Twisted Failure."""

    def __init__(self, matches: bool, value: str) -> None:
        self._matches = matches
        self.value = value

    def check(self, *types: type) -> type | None:
        return types[0] if self._matches else None


def test_a_robots_block_is_recorded_as_policy_not_as_an_error() -> None:
    """These two must stay distinguishable.

    A robots block is a deliberate decision that will never succeed on retry; a
    fetch failure is an incident that might. Collapsing them into one bucket
    would make a compliance choice look identical to an outage, and a reviewer
    could not tell a correctly behaving pipeline from a quietly broken one.
    """
    assert classify_failure(FakeFailure(True, "ignored")) == "robots_disallowed"


def test_any_other_failure_carries_its_cause() -> None:
    reason = classify_failure(FakeFailure(False, "404 Not Found"))
    assert reason.startswith("fetch_failed:")
    assert "404" in reason


def test_a_timeout_is_reported_with_its_cause_too() -> None:
    """A miss with no stated reason is indistinguishable from data that never
    existed, which is precisely what the brief's accounting rule forbids."""
    assert "TimeoutError" in classify_failure(FakeFailure(False, "TimeoutError: 60s"))


# --------------------------------------------------------------------------
# Requirement 10: failed downloads are logged with their URL and error code
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeHttpError:
    def __init__(self, status: int) -> None:
        self.response = FakeResponse(status)


def test_an_http_failure_reports_its_status_code() -> None:
    """The brief asks for error *codes*, not just error text.

    The exception's string usually contains the status as prose, which cannot
    be filtered, counted or alerted on. Surfacing it as an integer is what makes
    "how many 404s did this run see?" a query rather than a grep.
    """
    failure = FakeFailure(False, "404 Not Found")
    failure.value = FakeHttpError(404)  # type: ignore[assignment]
    assert failure_status(failure) == 404


def test_a_failure_that_never_got_a_response_has_no_status() -> None:
    """DNS failures, connection timeouts and robots refusals never reach a
    response, so None is the honest answer rather than a fabricated 0."""
    assert failure_status(FakeFailure(True, "ignored by robots")) is None
    assert failure_status(FakeFailure(False, "TimeoutError")) is None
