"""Pure parsing of the two page shapes this site serves.

Every function here takes HTML text and returns plain data. Nothing performs
I/O, so the whole parsing layer is unit-testable offline against captured
fixtures -- which is what makes it possible to prove the parsers handle all
three document shapes without touching the live site during a test run.

The spider is then only responsible for fetching and for persistence, and any
bug in understanding the site's markup is reproducible from a file on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag

DocType = Literal["html", "pdf", "html+pdf"]

# Present on every page as site furniture, never the decision itself. Matched on
# the filename so a path change elsewhere on the site does not defeat the filter.
_CHROME_FILENAMES = frozenset({"cookie_policy.pdf", "decisions_information_guide.pdf"})

_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".rtf")

# "Shows 1 to 10 of  234  results" -- the live markup contains newlines and runs
# of spaces between every number, so whitespace is collapsed before matching.
_COUNT = re.compile(r"Shows\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+results?", re.I)
_WHITESPACE = re.compile(r"\s+")


class ParseError(ValueError):
    """Raised when a page does not match the structure this module expects.

    Deliberately loud. A parser that silently returns nothing when the site
    changes its markup would let a crawl report success having scraped zero
    documents, which is the single most dangerous failure mode here.
    """


@dataclass(frozen=True, slots=True)
class ListingRecord:
    """One search result. `url` is site-relative, exactly as the page gives it."""

    identifier: str
    url: str
    decision_date: date
    description: str


@dataclass(frozen=True, slots=True)
class ListingPage:
    """One page of search results.

    `total_reported` is the site's own count for the whole query. It is the
    denominator for the brief's accounting rule -- scrape 200, or 200 minus X
    with every X explained -- and it drives pagination, because the pager only
    renders a sliding window of ten page links even when there are 24 pages.
    """

    total_reported: int
    shown_from: int
    shown_to: int
    records: tuple[ListingRecord, ...]

    @property
    def is_empty(self) -> bool:
        return not self.records


@dataclass(frozen=True, slots=True)
class Attachment:
    """A downloadable document linked from a case page."""

    url: str
    filename: str


@dataclass(frozen=True, slots=True)
class CasePage:
    """What a single case page offers, and in which of the three shapes.

    `content_length` is the length of the extracted text. It is exposed rather
    than reduced to a boolean so callers can flag suspiciously thin pages: a
    decision whose body is 40 characters is not a parse failure, but it is worth
    a quality flag in the curated zone.
    """

    identifier: str
    title: str
    doc_type: DocType
    attachments: tuple[Attachment, ...]
    content_length: int


def _text(node: Tag | None) -> str:
    return _WHITESPACE.sub(" ", node.get_text(" ", strip=True)).strip() if node else ""


def _attr(node: Tag | None, name: str) -> str:
    """Read a single-valued attribute, tolerating bs4's str | list return type."""
    if node is None:
        return ""
    value = node.get(name)
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _parse_ddmmyyyy(value: str) -> date:
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", value)
    if not match:
        raise ParseError(f"unrecognised date {value!r}; expected dd/mm/yyyy")
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:  # e.g. 31/02/2024
        raise ParseError(f"impossible date {value!r}") from exc


def parse_listing(html: str) -> ListingPage:
    """Parse one page of search results.

    A search that legitimately matched nothing renders `div.item-list` empty,
    with no count and no pager. That is returned as a valid empty page. A
    response with no `item-list` at all is not an empty result -- it is an error
    page, a redirect, or a markup change -- and raises, so a broken partition
    can never be mistaken for a genuinely empty one.
    """
    soup = BeautifulSoup(html, "lxml")

    # The page renders TWO div.item-list containers: an empty placeholder first,
    # then the populated `item-list search-list`. Scoping extraction to the first
    # match silently returns zero records on every page while the count still
    # parses -- a crawl that reports "234 expected, 0 scraped" indefinitely.
    #
    # So presence of any div.item-list is the "this is a results page" signal,
    # and the items are selected document-wide. li.each-item appears nowhere
    # else on the page, so this is both simpler and not over-broad.
    if not soup.select("div.item-list"):
        raise ParseError("no div.item-list; this is not a search results page")

    items = soup.select("li.each-item")

    head = _text(soup.select_one("div.searchhead"))
    match = _COUNT.search(head)
    if match is None:
        if items:
            raise ParseError(f"results present but no parseable count in {head!r}")
        return ListingPage(total_reported=0, shown_from=0, shown_to=0, records=())

    shown_from, shown_to, total = (int(g) for g in match.groups())
    records = tuple(_parse_listing_item(item) for item in items)
    return ListingPage(total, shown_from, shown_to, records)


def _parse_listing_item(item: Tag) -> ListingRecord:
    heading = item.select_one("h2.title")
    link = item.select_one("h2.title a[href]")
    if heading is None or link is None:
        raise ParseError("listing item has no h2.title link")

    # The `title` attribute carries the full identifier; the anchor text is the
    # same value but is the one the site would truncate if it ever styled it.
    identifier = _attr(heading, "title") or _text(link)
    if not identifier:
        raise ParseError("listing item has an empty identifier")

    description_node = item.select_one("p.description")
    # Same reasoning: the attribute holds the untruncated string.
    description = _attr(description_node, "title") or _text(description_node)

    return ListingRecord(
        identifier=identifier.strip(),
        url=_attr(link, "href"),
        decision_date=_parse_ddmmyyyy(_text(item.select_one("span.date"))),
        description=description.strip(),
    )


def _is_document_link(href: str) -> bool:
    path = urlsplit(href).path.lower()
    if not path.endswith(_DOCUMENT_SUFFIXES):
        return False
    return unquote(path.rsplit("/", 1)[-1]) not in _CHROME_FILENAMES


def parse_case_page(html: str) -> CasePage:
    """Classify one case page and list the documents it offers.

    The three shapes are distinguished by two independent signals -- whether
    `div.content` holds text, and whether any non-chrome document is linked:

        content empty,     attachment present  -> "pdf"       (EAT imports)
        content non-empty, attachment present  -> "html+pdf"  (older Equality Tribunal)
        content non-empty, no attachment       -> "html"      (WRC, Labour Court)

    Note that `div.content` is *present but empty* on EAT pages, so presence
    alone proves nothing; emptiness is the signal. Links are collected from both
    `div.content` and `div.related-items`, because the two eras put them in
    different places: EAT in a related-items download panel, the Equality
    Tribunal inside a plain list in the body.
    """
    soup = BeautifulSoup(html, "lxml")

    # `h1.page-title` specifically: case bodies contain their own <h1> headings,
    # so a bare `h1` selector picks up the first section heading of the decision.
    identifier = _text(soup.select_one("h1.page-title"))
    if not identifier:
        raise ParseError("no h1.page-title; this is not a case page")

    content = soup.select_one("div.content")
    content_text = _text(content)

    attachments: list[Attachment] = []
    seen: set[str] = set()
    for scope in (content, soup.select_one("div.related-items")):
        if scope is None:
            continue
        for anchor in scope.find_all("a", href=True):
            href = _attr(anchor, "href")
            if not _is_document_link(href) or href in seen:
                continue
            seen.add(href)
            filename = unquote(urlsplit(href).path.rsplit("/", 1)[-1])
            attachments.append(Attachment(url=href, filename=filename))

    has_content = bool(content_text)
    if attachments:
        doc_type: DocType = "html+pdf" if has_content else "pdf"
    else:
        doc_type = "html"

    # The decision's own first heading, where it has one; falls back to the
    # identifier so this field is never empty.
    inner = soup.select_one("div.content h1, div.content h2")
    return CasePage(
        identifier=identifier,
        title=_text(inner) or identifier,
        doc_type=doc_type,
        attachments=tuple(attachments),
        content_length=len(content_text),
    )
