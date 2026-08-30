"""URL construction tests.

The search endpoint is the one piece of the site the crawl cannot work without,
so its contract is pinned here rather than discovered at runtime.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from kedra_scraper.urls import absolute_url, format_date, page_count, search_url


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_dates_are_rendered_day_first() -> None:
    """The site's form uses dd/mm/yyyy.

    6 May rendered month-first becomes 05/06 -- a valid-looking date five months
    away, which the site would accept and silently return the wrong range for.
    """
    assert format_date(date(2024, 5, 6)) == "06/05/2024"


def test_search_url_carries_every_parameter_the_form_sends() -> None:
    url = search_url(body_id=15376, start=date(2024, 1, 1), end=date(2024, 1, 31))
    params = _params(url)
    assert params["decisions"] == ["1"]
    assert params["from"] == ["01/01/2024"]
    assert params["to"] == ["31/01/2024"]
    assert params["body"] == ["15376"]
    assert params["pageNumber"] == ["1"]
    # Sent empty because the live form sends it, rather than relying on the
    # endpoint tolerating its absence.
    assert params["legislationsub"] == [""]


def test_exactly_one_body_is_ever_requested() -> None:
    """Multi-valued `body` does not union the bodies -- it disables the filter.

    A URL carrying two body values silently returns every body's records, so a
    crawl would appear to succeed while scraping entirely the wrong data.
    """
    url = search_url(body_id=3, start=date(2024, 1, 1), end=date(2024, 1, 31))
    assert _params(url)["body"] == ["3"]


def test_page_number_is_carried_through() -> None:
    url = search_url(body_id=2, start=date(2010, 12, 1), end=date(2010, 12, 31), page=7)
    assert _params(url)["pageNumber"] == ["7"]


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="page must be 1 or greater"):
        search_url(body_id=1, start=date(2024, 1, 1), end=date(2024, 1, 2), page=0)
    with pytest.raises(ValueError, match="is after end"):
        search_url(body_id=1, start=date(2024, 2, 1), end=date(2024, 1, 1))


def test_page_count_rounds_up() -> None:
    """234 results at ten per page is 24 pages, not 23.

    Integer division would drop the final partial page -- four records for this
    range, and a silent shortfall the reconciliation would then have to explain.
    """
    assert page_count(234) == 24
    assert page_count(230) == 23
    assert page_count(1) == 1


def test_page_count_of_no_results_is_zero_pages() -> None:
    """An empty search must schedule no follow-up requests at all."""
    assert page_count(0) == 0


def test_relative_hrefs_become_absolute() -> None:
    assert absolute_url("/en/cases/2024/january/adj-1.html").startswith("https://")


def test_absolute_hrefs_are_left_alone() -> None:
    """Attachment links are sometimes already absolute; re-prefixing corrupts them."""
    original = "https://www.workplacerelations.ie/en/eat_import/2010/12/x.pdf"
    assert absolute_url(original) == original
