"""Building search URLs for the site's decision search.

The single most useful thing discovered about this site is that its search is
fully GET-addressable. It is ASP.NET WebForms and the form does post back with a
`__VIEWSTATE`, but the POST 302s to a plain querystring, and that URL works
standalone with no session, no cookies and no viewstate.

That is why this project needs no browser automation and no form replay: a
crawl is just a sequence of ordinary GETs whose parameters are computed here.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

BASE_URL = "https://www.workplacerelations.ie"
SEARCH_PATH = "/en/search/"

# The site returns a fixed ten results per page. A `pageSize` parameter is
# accepted and then ignored, so pagination arithmetic must assume ten and the
# crawl cannot be made cheaper by asking for larger pages.
PAGE_SIZE = 10


def format_date(value: date) -> str:
    """Render a date the way the search form does: dd/mm/yyyy.

    Day-first. The site also accepts ISO, but matching the form's own format
    keeps a hand-constructed URL identical to one the site would produce, which
    matters when reproducing a crawl by hand to debug it.
    """
    return value.strftime("%d/%m/%Y")


def search_url(*, body_id: int, start: date, end: date, page: int = 1) -> str:
    """Build one search results URL.

    `legislationsub` is sent empty because the live form always sends it; the
    endpoint tolerates its absence, but sending exactly what a browser sends
    avoids depending on that tolerance.

    One body per call, deliberately. Passing `body` twice does not union the two
    -- it silently degrades to no filter at all and returns every body's
    records, which would look like a successful crawl of the wrong data.
    """
    if page < 1:
        raise ValueError(f"page must be 1 or greater, got {page}")
    if start > end:
        raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")

    query = urlencode(
        {
            "decisions": 1,
            "from": format_date(start),
            "to": format_date(end),
            "legislationsub": "",
            "body": body_id,
            "pageNumber": page,
        }
    )
    return f"{BASE_URL}{SEARCH_PATH}?{query}"


def page_count(total_results: int, page_size: int = PAGE_SIZE) -> int:
    """How many pages a result total spans.

    Needed because the pager only renders a sliding window of ten page links,
    so the last visible link is not the last page. For 234 results that window
    ends at 10 while the real answer is 24 -- following the pager would silently
    drop 140 records.
    """
    if total_results <= 0:
        return 0
    return -(-total_results // page_size)


def absolute_url(href: str) -> str:
    """Turn a site-relative href into an absolute URL.

    Links in the markup are relative (`/en/cases/...`). Kept as a named function
    rather than inlined so the base URL has exactly one definition.
    """
    if href.startswith(("http://", "https://")):
        return href
    return f"{BASE_URL}{href}" if href.startswith("/") else f"{BASE_URL}/{href}"
