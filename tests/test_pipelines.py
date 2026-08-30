"""Tests for the pipeline logic that does not touch storage.

`_promote_primary` decides what a record claims about itself -- which file it
points at, and whether it is complete. Getting that wrong is how a partially
retrieved document comes to look fully scraped, so it is tested directly rather
than only through an end-to-end crawl.
"""

from __future__ import annotations

from datetime import date

from kedra_scraper.scraper.items import DecisionItem, StoredFile
from kedra_scraper.scraper.pipelines import ObjectStoragePipeline


def _item(*files: StoredFile) -> DecisionItem:
    return DecisionItem(
        identifier="DEC-E2000-14",
        body="equality_tribunal",
        body_id=1,
        title="A decision",
        description="",
        decision_date=date(2000, 5, 1),
        partition_date="2000-05",
        partition_start=date(2000, 5, 1),
        partition_end=date(2000, 5, 31),
        source_url="https://example.invalid/case.html",
        doc_type="html+pdf",
        run_id="run",
        scraper_version="0.1.0",
        files=list(files),
    )


def _stored(kind: str, *, path: str = "", skipped: str = "") -> StoredFile:
    return StoredFile(
        kind=kind,
        url="https://example.invalid/f",
        filename="f.pdf",
        path=path,
        skipped_reason=skipped,
        file_hash="sha256:aa",
        content_hash="sha256:bb",
        size=10,
        content_type="application/pdf",
    )


def test_a_fully_retrieved_record_is_ok_and_points_at_its_primary() -> None:
    item = _item(_stored("primary", path="s3://landing/primary.pdf"))
    ObjectStoragePipeline._promote_primary(item)
    assert item.status == "ok"
    assert item.file_path == "s3://landing/primary.pdf"
    assert item.error == ""


def test_a_skipped_primary_with_a_stored_secondary_is_partial() -> None:
    """The Equality Tribunal case, and the reason `partial` exists.

    The PDF is robots-disallowed but the HTML abstract is not. Marking this
    `ok` would claim a document we never fetched; dropping the record would
    discard an abstract we are entitled to keep. So it points at what was
    actually stored and carries the reason the rest is missing.
    """
    item = _item(
        _stored("secondary", path="s3://landing/abstract.html"),
        _stored("primary", skipped="robots_disallowed"),
    )
    ObjectStoragePipeline._promote_primary(item)
    assert item.status == "partial"
    assert item.file_path == "s3://landing/abstract.html"
    assert item.error == "robots_disallowed"


def test_a_record_with_nothing_stored_is_failed_not_silently_empty() -> None:
    """An empty file_path that still claimed `ok` would satisfy the record
    count while pointing at nothing, which is exactly the dangling-pointer
    state the pipeline ordering exists to prevent."""
    item = _item(_stored("primary", skipped="fetch_failed: 404"))
    ObjectStoragePipeline._promote_primary(item)
    assert item.status == "failed"
    assert item.file_path == ""
    assert "404" in item.error


def test_the_primary_wins_even_when_a_secondary_was_stored_first() -> None:
    """Order in `files` reflects fetch order, not authority.

    The HTML page is appended before its attachment is downloaded, so a naive
    "first stored file" rule would make every Equality Tribunal record point at
    the abstract even when the PDF was retrieved successfully.
    """
    item = _item(
        _stored("secondary", path="s3://landing/abstract.html"),
        _stored("primary", path="s3://landing/decision.pdf"),
    )
    ObjectStoragePipeline._promote_primary(item)
    assert item.status == "ok"
    assert item.file_path == "s3://landing/decision.pdf"


def test_multiple_skips_are_all_reported() -> None:
    """A record missing two files should say so, not just name the first."""
    item = _item(
        _stored("primary", skipped="robots_disallowed"),
        _stored("secondary", skipped="fetch_failed: timeout"),
    )
    ObjectStoragePipeline._promote_primary(item)
    assert item.status == "failed"
    assert "robots_disallowed" in item.error
    assert "timeout" in item.error


def test_primary_property_ignores_skipped_and_empty_entries() -> None:
    """`primary` must mean "retrieved", not merely "declared"."""
    item = _item(_stored("primary", skipped="robots_disallowed"))
    assert item.primary is None
