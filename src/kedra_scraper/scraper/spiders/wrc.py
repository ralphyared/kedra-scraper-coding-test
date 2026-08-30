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
from kedra_scraper.logging_setup import configure_logging, get_logger, new_run_id
from kedra_scraper.naming import document_id
from kedra_scraper.parsers import ListingRecord, parse_case_page, parse_listing
from kedra_scraper.scraper.items import DecisionItem, StoredFile
from kedra_scraper.storage.objects import content_type_for
from kedra_scraper.urls import absolute_url, page_count, search_url

log = get_logger(__name__)


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

        # Reconciliation counters. `total_reported` is the site's own figure and
        # is the denominator the brief's accounting rule is judged against.
        self.total_reported = 0
        self.records_seen = 0
        self.items_emitted = 0
        self.failures = 0
        self.skipped_files = 0

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
        """Classify one case page and queue whatever still needs downloading."""
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

        return self._fetch_attachments(item, list(case.attachments))

    def _fetch_attachments(self, item: DecisionItem, pending: list[Any]) -> Iterator[Any]:
        """Download attachments one at a time, then emit the finished item.

        Sequential rather than parallel because a decision is a single record:
        the item has to be complete before it reaches the pipelines, and
        chaining is a far simpler way to guarantee that than collecting parallel
        responses and joining them.
        """
        if not pending:
            self.items_emitted += 1
            self._document_ids.add(document_id(item.body, item.identifier))
            yield item
            return

        attachment, rest = pending[0], pending[1:]
        yield scrapy.Request(
            absolute_url(attachment.url),
            callback=self.parse_attachment,
            errback=self.attachment_failed,
            cb_kwargs={"item": item, "attachment": attachment, "pending": rest},
            # The same PDF can legitimately be linked from more than one
            # decision, and each needs its own copy recorded.
            dont_filter=True,
        )

    def parse_attachment(
        self, response: Response, item: DecisionItem, attachment: Any, pending: list[Any]
    ) -> Iterator[Any]:
        item.files.append(
            StoredFile(
                kind="primary",
                url=response.url,
                filename=attachment.filename,
                data=response.body,
                content_type=content_type_for(attachment.filename),
            )
        )
        log.info(
            "document_downloaded",
            identifier=item.identifier,
            url=response.url,
            bytes=len(response.body),
            doc_type=item.doc_type,
        )
        yield from self._fetch_attachments(item, pending)

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

        robots_blocked = failure.check(IgnoreRequest) is not None
        reason = "robots_disallowed" if robots_blocked else f"fetch_failed: {failure.value}"

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
        yield from self._fetch_attachments(item, pending)

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
        unaccounted = self.total_reported - self.items_emitted - self.failures
        duplicates = self.items_emitted - len(self._document_ids)
        log.info(
            "partition_finished",
            body=self.body.slug,
            partition_date=self.partition_label,
            reason=reason,
            total_reported=self.total_reported,
            records_seen=self.records_seen,
            items_emitted=self.items_emitted,
            unique_documents=len(self._document_ids),
            duplicate_rows=duplicates,
            failures=self.failures,
            files_skipped=self.skipped_files,
            unaccounted=unaccounted,
            reconciled=unaccounted == 0,
            run_id=self.run_id,
        )
