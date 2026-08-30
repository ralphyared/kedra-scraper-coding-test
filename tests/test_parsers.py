"""Parser tests, run entirely against real captured pages.

The fixtures are unmodified responses from workplacerelations.ie. Testing
against synthetic HTML would only prove the parsers handle markup we invented;
these prove they handle the markup the site actually serves, including its
irregular whitespace, its duplicate containers, and its three document shapes.
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import read_fixture
from kedra_scraper.parsers import ParseError, parse_case_page, parse_listing

LISTING = "listing_wrc_2024-01_page1.html"
ZERO_RESULTS = "listing_zero_results.html"


# --------------------------------------------------------------------------
# Search result listings
# --------------------------------------------------------------------------


def test_listing_reports_the_site_s_own_total() -> None:
    """The total drives both pagination and the found-vs-scraped reconciliation."""
    page = parse_listing(read_fixture(LISTING))
    assert page.total_reported == 234
    assert (page.shown_from, page.shown_to) == (1, 10)


def test_listing_extracts_every_record_on_the_page() -> None:
    """Regression: the page contains TWO div.item-list containers.

    The first is an empty placeholder; only the second holds results. Scoping
    extraction to the first match returns zero records while the count still
    parses correctly, so a crawl would report "234 expected, 0 scraped" on every
    page indefinitely -- looking like a site problem rather than a parser bug.
    """
    page = parse_listing(read_fixture(LISTING))
    assert len(page.records) == 10
    assert not page.is_empty


def test_listing_page_size_implies_the_page_count() -> None:
    """Pagination must be driven by the total, not by the pager's links.

    The pager renders only a sliding window of ten page numbers, but 234 results
    at ten per page is 24 pages. Trusting the highest visible page link would
    silently truncate the crawl at page 10, losing 140 records.
    """
    page = parse_listing(read_fixture(LISTING))
    assert len(page.records) == 10
    assert -(-page.total_reported // 10) == 24


def test_listing_record_fields() -> None:
    page = parse_listing(read_fixture(LISTING))
    first = page.records[0]
    assert first.identifier == "ADJ-00047352"
    assert first.decision_date == date(2024, 1, 31)
    assert first.url == "/en/cases/2024/january/adj-00047352.html"
    assert first.description == "Car Valet V Motor Garage"


def test_listing_preserves_identifiers_containing_spaces() -> None:
    """`IR - SC - 00001785` is a real identifier. It must survive verbatim.

    Normalising it here would corrupt the raw value that the metadata record is
    required to preserve; the safe form is derived separately, in naming.py.
    """
    page = parse_listing(read_fixture(LISTING))
    identifiers = [r.identifier for r in page.records]
    assert "IR - SC - 00001785" in identifiers


def test_listing_dates_are_parsed_as_day_first() -> None:
    """dd/mm/yyyy, not mm/dd/yyyy.

    Every date on this page is 31/01/2024. Read month-first that is invalid,
    which is why an ambiguous date like 05/06/2024 would otherwise be silently
    misfiled by five months without any error.
    """
    page = parse_listing(read_fixture(LISTING))
    assert {r.decision_date for r in page.records} == {date(2024, 1, 31)}


def test_zero_results_is_a_valid_empty_page_not_an_error() -> None:
    """An empty div.item-list means the search genuinely matched nothing."""
    page = parse_listing(read_fixture(ZERO_RESULTS))
    assert page.total_reported == 0
    assert page.records == ()
    assert page.is_empty


def test_a_page_that_is_not_a_result_list_raises() -> None:
    """The distinction that makes an empty partition trustworthy.

    A failed fetch, a redirect to the home page, or a markup change has no
    div.item-list at all. Returning "0 records" for those would let a broken
    partition report success, and the reconciliation would agree with it.
    """
    with pytest.raises(ParseError, match="not a search results page"):
        parse_listing("<html><body><p>Service unavailable</p></body></html>")


# --------------------------------------------------------------------------
# Case pages: the three document shapes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected_type", "identifier", "attachment_count"),
    [
        ("case_wrc_inline.html", "html", "ADJ-00047352", 0),
        ("case_labour_court_inline.html", "html", "LCR22912", 0),
        (
            "case_eat_pdf_only.html",
            "pdf",
            "RP2147/2009, MN1794/2009, WT796/2009",
            1,
        ),
        ("case_equality_tribunal_html_plus_pdf.html", "html+pdf", "DEC-E2000-14", 1),
    ],
)
def test_case_page_shapes(
    fixture: str, expected_type: str, identifier: str, attachment_count: int
) -> None:
    """All three shapes the site serves, one real page each.

    Both branches of the brief's requirement 6 are live: 6a (download the
    attached document as-is) and 6b (save the rendered page), split by body and
    era rather than by anything declared in the markup.
    """
    page = parse_case_page(read_fixture(fixture))
    assert page.doc_type == expected_type
    assert page.identifier == identifier
    assert len(page.attachments) == attachment_count


def test_pdf_only_page_has_empty_content_but_an_attachment() -> None:
    """`div.content` is present-but-empty on EAT pages.

    Presence therefore proves nothing; emptiness is the signal. A check written
    as `if soup.select_one("div.content")` would classify every EAT import as
    inline HTML and store an empty page instead of the actual decision.
    """
    page = parse_case_page(read_fixture("case_eat_pdf_only.html"))
    assert page.content_length == 0
    assert page.doc_type == "pdf"
    assert page.attachments[0].filename.endswith(".pdf")
    assert "/eat_import/" in page.attachments[0].url


def test_html_plus_pdf_page_keeps_both_artifacts() -> None:
    """Older Equality Tribunal pages carry an abstract *and* a source PDF.

    The PDF is the authoritative document, but the HTML abstract is real content
    and is not discarded -- hence a distinct doc_type rather than folding this
    case into "pdf".
    """
    page = parse_case_page(read_fixture("case_equality_tribunal_html_plus_pdf.html"))
    assert page.doc_type == "html+pdf"
    assert page.content_length > 0
    assert page.attachments[0].filename == "DEC-E2000-14.pdf"


def test_site_chrome_pdfs_are_not_mistaken_for_documents() -> None:
    """Every page links the cookie policy and an information guide.

    Both are PDFs. Collecting document links naively would attach two irrelevant
    files to all 62k records and classify every inline-HTML page as "html+pdf".
    """
    page = parse_case_page(read_fixture("case_wrc_inline.html"))
    assert page.attachments == ()
    assert page.doc_type == "html"


def test_identifier_comes_from_the_page_title_not_the_body() -> None:
    """Decision bodies contain their own <h1> headings.

    A bare `h1` selector returns the first heading *inside* the decision text,
    so records would be keyed on a sentence like "ADJUDICATION OFFICER
    Recommendation..." instead of on ADJ-00047352.
    """
    page = parse_case_page(read_fixture("case_wrc_inline.html"))
    assert page.identifier == "ADJ-00047352"
    assert "ADJUDICATION" not in page.identifier


def test_a_page_that_is_not_a_case_page_raises() -> None:
    with pytest.raises(ParseError, match="not a case page"):
        parse_case_page("<html><body><div class='content'>hi</div></body></html>")
