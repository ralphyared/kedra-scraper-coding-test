"""Compliance tests against the site's real robots.txt.

The project's stated posture is to obey robots.txt and document the reasoning,
so "are we allowed to fetch this?" is a claim that has to be *proved* rather
than asserted in prose. These tests pin that proof against a captured copy of
the live file, using Protego -- the same parser Scrapy itself uses, so the
answers here are the answers the crawler will actually get.

The finding that matters is at the bottom: one of the four bodies publishes its
documents under a path that IS disallowed. The plan for this project claimed
otherwise, and building on that claim would have meant knowingly violating
robots.txt for a whole body's attachments.
"""

from __future__ import annotations

import pytest
from protego import Protego

from conftest import FIXTURES

UA = "kedra-scraper/0.1"
BASE = "https://www.workplacerelations.ie"


@pytest.fixture(scope="module")
def robots() -> Protego:
    return Protego.parse((FIXTURES / "robots.txt").read_text(encoding="utf-8"))


def _can(robots: Protego, path: str) -> bool:
    return bool(robots.can_fetch(BASE + path, UA))


# --------------------------------------------------------------------------
# Permitted
# --------------------------------------------------------------------------


def test_the_search_endpoint_is_not_restricted_at_all(robots: Protego) -> None:
    """`/en/search/` appears nowhere in robots.txt.

    This is the entry point for every crawl, so it is worth an explicit test
    rather than being inferred from the absence of a rule.
    """
    assert _can(robots, "/en/search/?decisions=1&from=01/01/2024&to=31/01/2024&body=15376")


def test_lowercase_case_pages_are_permitted(robots: Protego) -> None:
    """robots.txt disallows `/en/Cases/`; the real links are `/en/cases/`.

    Robots path matching is case-sensitive, so these are genuinely different
    rules and the lowercase form is not covered. The site reinforces this: case
    pages return `X-Robots-Tag: index, follow`, which would be incoherent if it
    also intended to forbid crawling them.
    """
    assert _can(robots, "/en/cases/2024/january/adj-00047352.html")
    assert _can(robots, "/en/cases/2024/january/lcr22912.html")


def test_the_capitalised_form_really_is_the_one_that_is_blocked(robots: Protego) -> None:
    """Guards the reasoning above rather than the code.

    If this ever passes, case-sensitivity is not behaving as assumed and the
    whole permission argument for crawling case pages needs revisiting.
    """
    assert not _can(robots, "/en/Cases/2024/january/adj-00047352.html")


def test_eat_attachments_are_permitted(robots: Protego) -> None:
    """EAT PDFs are linked as lowercase `/en/eat_import/`, unlike the rule."""
    assert _can(robots, "/en/eat_import/2010/12/75d3358e-f145-40d5-9922-da2822791892.pdf")


# --------------------------------------------------------------------------
# Not permitted -- the finding
# --------------------------------------------------------------------------


def test_equality_tribunal_attachments_are_disallowed(robots: Protego) -> None:
    """The Equality Tribunal does NOT follow the lowercase pattern.

    Its PDFs are linked with the capitalised path preserved:

        /en/Equality_Tribunal_Import/Database-of-Decisions/2000/DEC-E2000-14.pdf

    which matches `Disallow: /en/Equality_Tribunal_Import/` exactly. Unlike the
    other three bodies, these attachments are genuinely off limits.

    The consequence is deliberate and must stay visible: for these records the
    HTML abstract is stored and the PDF is skipped with an explicit reason, so
    the shortfall is accounted for under the brief's rule that every unscraped
    document is logged with why. Silently dropping them would look identical to
    a download bug, and fetching them anyway would violate a stated posture.
    """
    assert not _can(
        robots,
        "/en/Equality_Tribunal_Import/Database-of-Decisions/2000/DEC-E2000-14.pdf",
    )


def test_labour_court_import_path_is_also_disallowed(robots: Protego) -> None:
    """Recorded because it is a latent version of the same trap.

    No captured fixture links to `/en/Labour_Court_Import/` -- current Labour
    Court decisions are inline HTML -- but the rule exists, so older records may
    reference it. The skip is therefore handled generically by URL, never by
    special-casing one body.
    """
    assert not _can(robots, "/en/Labour_Court_Import/some-decision.pdf")


def test_the_crawler_must_not_assume_a_body_s_links_are_lowercase(robots: Protego) -> None:
    """The generalised lesson, pinned so it cannot quietly regress.

    Two of the four bodies serve attachments under a disallowed prefix. Any code
    that decides permission from the body rather than from the URL will be wrong
    for half of them.
    """
    permitted = "/en/eat_import/2010/12/x.pdf"
    blocked = "/en/Equality_Tribunal_Import/Database-of-Decisions/2000/x.pdf"
    assert _can(robots, permitted)
    assert not _can(robots, blocked)
