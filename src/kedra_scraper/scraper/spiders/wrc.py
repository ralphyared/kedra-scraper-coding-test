"""The decision spider: one body, one date range, one run.

Scope is deliberately one partition per crawl. The orchestrator runs many of
them; keeping the spider itself single-partition means a failure is isolated to
one body-month, a retry re-runs exactly that unit, and the record counts for
each partition reconcile independently.

The spider only fetches and assembles. Every decision about what the markup
means lives in `kedra_scraper.parsers`, and every decision about where bytes go
lives in the pipelines.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any, ClassVar
from urllib.parse import urlsplit

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

from kedra_scraper import __version__
from kedra_scraper.bodies import resolve
from kedra_scraper.config import get_settings
from kedra_scraper.hashing import content_hash
from kedra_scraper.logging_setup import configure_logging, get_logger, new_run_id
from kedra_scraper.naming import document_id
from kedra_scraper.parsers import ListingRecord, parse_case_page, parse_listing
from kedra_scraper.scraper.items import DecisionItem, StoredFile
from kedra_scraper.storage.mongo import DecisionRepository
from kedra_scraper.storage.objects import content_type_for
from kedra_scraper.urls import absolute_url, page_count, search_url

log = get_logger(__name__)


def classify_failure(failure: Any) -> str:
    """Turn a download failure into the reason recorded against the document.

    A module-level function rather than a method so the classification can be
    unit-tested without standing up a spider, a reactor, or a database.

    The distinction it draws is the one that matters for the brief's accounting
    rule. `robots_disallowed` is a deliberate compliance decision and will never
    succeed on a retry; anything else is an incident that might. Collapsing them
    into one "failed" bucket would make a policy choice indistinguishable from an
    outage, and a reviewer could not tell whether the pipeline was behaving
    correctly or quietly broken.
    """
    if failure.check(IgnoreRequest) is not None:
        return "robots_disallowed"
    return f"fetch_failed: {failure.value}"


class WrcSpider(scrapy.Spider):
    """Crawl one body over one date range.

    Run standalone:

        scrapy crawl wrc -a body=wrc -a start=2024-01-01 -a end=2024-01-31
    """

    name = "wrc"
    allowed_domains: ClassVar[list[str]] = ["workplacerelations.ie"]

    def __init__(
        self,
        body: str = "wrc",
        start: str = "",
        end: str = "",
        partition: str = "",
        run_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not start or not end:
            raise ValueError("both -a start=YYYY-MM-DD and -a end=YYYY-MM-DD are required")

        self.body = resolve(body)
        self.start_date = date.fromisoformat(start)
        self.end_date = date.fromisoformat(end)
        if self.start_date > self.end_date:
            raise ValueError(f"start {start} is after end {end}")

        # Defaults to the calendar month of the start date. Dagster passes this
        # explicitly so the partition key it wrote is the one recorded, rather
        # than one re-derived here and possibly disagreeing at a month boundary.
        self.partition_label = (
            partition or f"{self.start_date.year:04d}-{self.start_date.month:02d}"
        )
        self.run_id = run_id or new_run_id()

        # Installed here, not at import time. Scrapy configures the root logger
        # while starting the crawler, which happens before the spider is
        # constructed -- so doing this any earlier would have Scrapy's plain-text
        # handlers replace ours, and the run would emit unparseable logs. Running
        # after it means our JSON formatter wins and Scrapy's own records are
        # rendered through it too.
        settings = get_settings()
        configure_logging(level=settings.log_level, log_dir=settings.log_dir, run_id=self.run_id)

        # Read access to what previous runs stored. The spider needs this to
        # decide whether a document is worth downloading again, which has to
        # happen before the attachment request is issued -- a pipeline would
        # only get the chance after the bytes were already on the wire.
        self._repo = DecisionRepository.from_settings(settings.mongo)

        # Reconciliation counters. Every listing row the site reported must end
        # up in exactly one of these buckets, which is what makes the brief's
        # accounting rule checkable rather than aspirational:
        #
        #   total_reported == duplicate_rows + skipped_unchanged
        #                     + items_emitted + failures
        self.total_reported = 0
        self.records_seen = 0
        self.items_emitted = 0
        self.failures = 0
        self.skipped_files = 0
        self.skipped_unchanged = 0
        self.duplicate_rows = 0
        self.conditional_hits = 0

        # The site lists a decision once per constituent claim number, so a
        # single joined decision such as "RP166/2010, RP317/2010, RP339/2010,
        # RP340/2010" occupies four listing rows that all resolve to the same
        # document. Tracking distinct ids is what lets the run explain why the
        # stored record count is legitimately lower than the reported total,
        # instead of leaving a silent shortfall for a reviewer to discover.
        self._document_ids: set[str] = set()

    # ----------------------------------------------------------------------
    # Listing pages
    # ----------------------------------------------------------------------

    async def start(self) -> Any:
        """Entry point. Scrapy 2.13+ replaced `start_requests` with this."""
        log.info(
            "partition_started",
            body=self.body.slug,
            partition_date=self.partition_label,
            date_from=self.start_date.isoformat(),
            date_to=self.end_date.isoformat(),
            run_id=self.run_id,
        )
        yield scrapy.Request(
            search_url(body_id=self.body.id, start=self.start_date, end=self.end_date, page=1),
            callback=self.parse_listing,
            cb_kwargs={"page": 1},
        )

    def parse_listing(self, response: Response, page: int) -> Iterator[Any]:
        """Parse one results page, then fan out to case pages.

        Pagination is computed from the reported total on the first page rather
        than followed link by link. The pager only renders a sliding window of
        ten page numbers, so following it would stop at page 10 of 24 and lose
        140 records without any error.
        """
        listing = parse_listing(response.text)

        if page == 1:
            self.total_reported = listing.total_reported
            pages = page_count(listing.total_reported)
            log.info(
                "listing_first_page",
                body=self.body.slug,
                partition_date=self.partition_label,
                total_reported=listing.total_reported,
                pages=pages,
            )
            for next_page in range(2, pages + 1):
                yield scrapy.Request(
                    search_url(
                        body_id=self.body.id,
                        start=self.start_date,
                        end=self.end_date,
                        page=next_page,
                    ),
                    callback=self.parse_listing,
                    cb_kwargs={"page": next_page},
                )

        self.records_seen += len(listing.records)
        log.info(
            "listing_page_parsed",
            body=self.body.slug,
            partition_date=self.partition_label,
            page=page,
            records_on_page=len(listing.records),
            total_reported=self.total_reported,
        )

        for record in listing.records:
            yield scrapy.Request(
                absolute_url(record.url),
                callback=self.parse_case,
                errback=self.case_failed,
                cb_kwargs={"record": record},
            )

    # ----------------------------------------------------------------------
    # Case pages
    # ----------------------------------------------------------------------

    def parse_case(self, response: Response, record: ListingRecord) -> Iterator[Any]:
        """Classify one case page, then decide whether it needs storing at all.

        Two cheap exits come before any attachment is requested, because that is
        the only point at which a download can still be avoided.
        """
        doc_id = document_id(self.body.slug, record.identifier)

        # 1. The same decision listed twice in this run. Not a change, not a
        #    skip -- just the site listing one joined decision once per claim
        #    number. Counted separately so it never looks like missing data.
        if doc_id in self._document_ids:
            self.duplicate_rows += 1
            log.debug("duplicate_listing_row", identifier=record.identifier, document_id=doc_id)
            return iter(())
        self._document_ids.add(doc_id)

        # 2. Already stored, and the page is byte-identical after normalisation.
        #
        #    The case page is the source of truth for what a decision consists
        #    of: the site regenerates it from its CMS, so a change to the text
        #    or to which documents are attached necessarily changes this page.
        #    An unchanged page therefore means an unchanged decision, and the
        #    attachment does not need re-fetching to establish that.
        #
        #    This comparison uses content_hash, not the raw bytes. The page
        #    embeds a per-request `Elapsed time` comment, so a raw comparison
        #    would report every document as changed on every run and this branch
        #    would never be taken -- the exact failure the two-hash design exists
        #    to prevent.
        page_hash = content_hash(response.body, is_html=True)
        existing = self._repo.get(doc_id)
        if (
            existing is not None
            and existing.get("page_hash") == page_hash
            and existing.get("status") in {"ok", "partial"}
        ):
            self._repo.touch(doc_id)
            self.skipped_unchanged += 1
            log.info(
                "record_unchanged_skipped",
                identifier=record.identifier,
                document_id=doc_id,
                content_hash=page_hash,
            )
            return iter(())

        case = parse_case_page(response.text)

        item = DecisionItem(
            identifier=record.identifier,
            body=self.body.slug,
            body_id=self.body.id,
            title=case.title,
            description=record.description,
            decision_date=record.decision_date,
            partition_date=self.partition_label,
            partition_start=self.start_date,
            partition_end=self.end_date,
            source_url=response.url,
            doc_type=case.doc_type,
            run_id=self.run_id,
            scraper_version=__version__,
            page_hash=page_hash,
        )

        # The rendered page is stored whenever it carries content. It is the
        # authoritative document when nothing is attached, and a secondary
        # artifact when a source PDF exists -- the older Equality Tribunal pages
        # carry a real abstract that would be lost if only the PDF were kept.
        if case.content_length > 0 or not case.attachments:
            filename = urlsplit(response.url).path.rsplit("/", 1)[-1] or "index.html"
            item.files.append(
                StoredFile(
                    kind="secondary" if case.attachments else "primary",
                    url=response.url,
                    filename=filename,
                    data=response.body,
                    content_type="text/html; charset=utf-8",
                )
            )

        return self._fetch_attachments(item, list(case.attachments), existing)

    @staticmethod
    def _previous_file(existing: dict[str, Any] | None, url: str) -> dict[str, Any] | None:
        """Find what a previous run stored for this attachment URL, if anything."""
        if not existing:
            return None
        for entry in existing.get("files", []):
            if entry.get("url", "").endswith(url) or url.endswith(entry.get("url", "")):
                return entry if entry.get("path") else None
        return None

    def _fetch_attachments(
        self, item: DecisionItem, pending: list[Any], existing: dict[str, Any] | None
    ) -> Iterator[Any]:
        """Download attachments one at a time, then emit the finished item.

        Sequential rather than parallel because a decision is a single record:
        the item has to be complete before it reaches the pipelines, and
        chaining is a far simpler way to guarantee that than collecting parallel
        responses and joining them.
        """
        if not pending:
            self.items_emitted += 1
            yield item
            return

        attachment, rest = pending[0], pending[1:]
        url = absolute_url(attachment.url)
        previous = self._previous_file(existing, url)

        headers = {}
        if previous and previous.get("etag"):
            # Attachments are the one place a conditional request works here.
            # Case pages send `Cache-Control: no-cache` with no validators, but
            # PDFs carry an ETag the server honours -- measured, because the same
            # responses also advertise Last-Modified and then ignore
            # If-Modified-Since, resending the full body. Using the wrong one
            # would look like working conditional GET while changing nothing.
            headers["If-None-Match"] = previous["etag"]

        yield scrapy.Request(
            url,
            callback=self.parse_attachment,
            errback=self.attachment_failed,
            headers=headers,
            cb_kwargs={
                "item": item,
                "attachment": attachment,
                "pending": rest,
                "existing": existing,
                "previous": previous,
            },
            # 304 is a success here, not an error. Without this Scrapy's
            # HttpErrorMiddleware treats any non-2xx as a failure and routes it
            # to the errback, where an unchanged file would be recorded as a
            # fetch failure.
            meta={"handle_httpstatus_list": [304]},
            # The same PDF can legitimately be linked from more than one
            # decision, and each needs its own copy recorded.
            dont_filter=True,
        )

    def parse_attachment(
        self,
        response: Response,
        item: DecisionItem,
        attachment: Any,
        pending: list[Any],
        existing: dict[str, Any] | None,
        previous: dict[str, Any] | None,
    ) -> Iterator[Any]:
        if response.status == 304 and previous is not None:
            # Unchanged. Carry the previous run's stored details forward with no
            # payload, so the storage pipeline has nothing to write and the
            # record still points at the object already in the landing zone.
            self.conditional_hits += 1
            item.files.append(
                StoredFile(
                    kind=previous.get("kind", "primary"),
                    url=previous["url"],
                    filename=previous["filename"],
                    content_type=previous.get("content_type", ""),
                    size=previous.get("size", 0),
                    file_hash=previous.get("file_hash", ""),
                    content_hash=previous.get("content_hash", ""),
                    path=previous["path"],
                    etag=previous.get("etag", ""),
                )
            )
            log.info(
                "document_not_modified",
                identifier=item.identifier,
                url=response.url,
                etag=previous.get("etag", ""),
            )
        else:
            item.files.append(
                StoredFile(
                    kind="primary",
                    url=response.url,
                    filename=attachment.filename,
                    data=response.body,
                    content_type=content_type_for(attachment.filename),
                    etag=(response.headers.get("ETag") or b"").decode("latin-1"),
                )
            )
            log.info(
                "document_downloaded",
                identifier=item.identifier,
                url=response.url,
                bytes=len(response.body),
                doc_type=item.doc_type,
            )
        yield from self._fetch_attachments(item, pending, existing)

    # ----------------------------------------------------------------------
    # Failure paths
    # ----------------------------------------------------------------------

    def attachment_failed(self, failure: Any) -> Iterator[Any]:
        """Record why a document was not retrieved, and keep the record.

        A robots-disallowed attachment is not an error: it is a deliberate
        compliance decision, and the Equality Tribunal publishes every PDF under
        a disallowed prefix. Dropping the whole decision would discard an
        abstract we are allowed to keep; pretending the file was fetched would
        be a lie. So the gap is recorded with its reason and the record is still
        emitted, which is exactly the brief's rule that any shortfall is
        explained rather than merely absent.
        """
        request = failure.request
        item: DecisionItem = request.cb_kwargs["item"]
        attachment = request.cb_kwargs["attachment"]
        pending: list[Any] = request.cb_kwargs["pending"]
        existing: dict[str, Any] | None = request.cb_kwargs.get("existing")

        reason = classify_failure(failure)

        item.files.append(
            StoredFile(
                kind="primary",
                url=absolute_url(attachment.url),
                filename=attachment.filename,
                skipped_reason=reason,
            )
        )
        self.skipped_files += 1
        log.warning(
            "document_skipped",
            identifier=item.identifier,
            url=absolute_url(attachment.url),
            reason=reason,
        )
        yield from self._fetch_attachments(item, pending, existing)

    def case_failed(self, failure: Any) -> None:
        """A case page that could not be fetched at all.

        Counted so the reconciliation at the end of the run can account for the
        difference between what the site reported and what was persisted.
        """
        self.failures += 1
        log.error(
            "case_fetch_failed",
            url=failure.request.url,
            error=str(failure.value),
            partition_date=self.partition_label,
        )

    # ----------------------------------------------------------------------
    # Reconciliation
    # ----------------------------------------------------------------------

    def closed(self, reason: str) -> None:
        """Report found versus scraped, and whether the difference is explained.

        This is the brief's accounting rule made mechanical: scrape everything,
        or scrape fewer and account for each miss. `reconciled` is the single
        boolean an orchestrator can assert on.

        Two different totals are reported, because they answer two different
        questions. `items_emitted` is measured against the site's own count and
        proves no listing row was dropped. `unique_documents` is what will
        actually be in the database, and is lower whenever the site lists one
        joined decision under several claim numbers. Reporting only the first
        would leave the stored count looking short; reporting only the second
        would hide a genuinely missed row.
        """
        accounted = (
            self.items_emitted + self.skipped_unchanged + self.duplicate_rows + self.failures
        )
        unaccounted = self.total_reported - accounted
        log.info(
            "partition_finished",
            body=self.body.slug,
            partition_date=self.partition_label,
            reason=reason,
            total_reported=self.total_reported,
            records_seen=self.records_seen,
            # Every listing row lands in exactly one of the next four buckets.
            items_emitted=self.items_emitted,
            skipped_unchanged=self.skipped_unchanged,
            duplicate_rows=self.duplicate_rows,
            failures=self.failures,
            unique_documents=len(self._document_ids),
            files_skipped=self.skipped_files,
            conditional_hits=self.conditional_hits,
            unaccounted=unaccounted,
            reconciled=unaccounted == 0,
            run_id=self.run_id,
        )
