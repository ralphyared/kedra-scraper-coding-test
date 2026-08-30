"""The item the spider produces: one per decision, whatever its shape.

A dataclass rather than `scrapy.Item`. Scrapy supports both through
itemadapter, and a dataclass gives real attribute names that mypy can check,
which matters because this object is assembled across several callbacks.

One item per decision, never one per file. A decision with an HTML abstract and
a source PDF is a single record with two entries in `files`, because the unit
the metadata store is keyed on is the decision, not the download.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class StoredFile:
    """One downloaded artifact belonging to a decision.

    `kind` distinguishes the authoritative document from supporting material:
    for a PDF-only EAT record the PDF is primary, while for an Equality Tribunal
    record the PDF is primary and the HTML abstract is secondary.
    """

    kind: str
    url: str
    filename: str
    data: bytes = b""
    content_type: str = ""
    size: int = 0
    file_hash: str = ""
    content_hash: str = ""
    path: str = ""

    # Set when a file was deliberately not fetched, so the gap is explained
    # rather than merely absent. Currently only "robots_disallowed".
    skipped_reason: str = ""


@dataclass
class DecisionItem:
    """Metadata plus payload for one decision, in flight through the pipelines.

    The `files` payloads are dropped by the storage pipeline once written, so
    the document that reaches Mongo carries pointers and hashes rather than
    bytes.
    """

    identifier: str
    body: str
    body_id: int
    title: str
    description: str
    decision_date: date
    partition_date: str
    partition_start: date
    partition_end: date
    source_url: str
    doc_type: str
    run_id: str
    scraper_version: str

    files: list[StoredFile] = field(default_factory=list)

    # Filled in by the pipelines.
    document_id: str = ""
    file_path: str = ""
    file_hash: str = ""
    content_hash: str = ""
    file_size: int = 0
    content_type: str = ""
    status: str = "ok"
    error: str = ""

    @property
    def primary(self) -> StoredFile | None:
        """The authoritative document, if one was actually retrieved."""
        for item in self.files:
            if item.kind == "primary" and item.data and not item.skipped_reason:
                return item
        return None
