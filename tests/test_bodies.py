"""Body registry tests.

The numeric ids are site data, not our invention. They are asserted explicitly
so that a typo in the registry fails here rather than manifesting as a crawl
that quietly scrapes the wrong tribunal.
"""

from __future__ import annotations

import pytest

from kedra_scraper.bodies import BODIES, by_id, resolve


def test_ids_match_the_values_the_search_form_publishes() -> None:
    """Read off the checkboxes in tests/fixtures/search_form_excerpt.html.

    They are not sequential and the WRC's is five digits, so they cannot be
    inferred or reconstructed -- only recorded and verified.
    """
    assert {b.slug: b.id for b in BODIES} == {
        "equality_tribunal": 1,
        "employment_appeals_tribunal": 2,
        "labour_court": 3,
        "workplace_relations_commission": 15376,
    }


def test_resolve_accepts_the_short_alias() -> None:
    """`--body wrc` is what anyone will actually type."""
    assert resolve("wrc").id == 15376
    assert resolve("eat").id == 2


def test_resolve_accepts_the_full_slug() -> None:
    assert resolve("labour_court").id == 3


def test_resolve_is_case_insensitive() -> None:
    assert resolve("WRC") == resolve("wrc")


def test_resolve_ignores_surrounding_whitespace() -> None:
    assert resolve("  lc  ").slug == "labour_court"


def test_an_unknown_body_fails_loudly_and_lists_the_valid_names() -> None:
    """The worst possible behaviour here is a silent fallback.

    A typo that resolved to "no filter" would return every body's records, and
    the crawl would look successful while scraping 62k documents instead of the
    requested few thousand.
    """
    with pytest.raises(ValueError, match="unknown body") as exc:
        resolve("labour-court")
    assert "labour_court" in str(exc.value)


def test_by_id_round_trips() -> None:
    for body in BODIES:
        assert by_id(body.id) is body


def test_unknown_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown body id"):
        by_id(999)


def test_slugs_and_aliases_are_globally_unique() -> None:
    """A duplicate would make one body permanently unreachable by name."""
    names = [name for body in BODIES for name in body.all_names]
    assert len(names) == len(set(names))


def test_bodies_are_immutable() -> None:
    """Slugs appear in Mongo `_id` values and object keys.

    Mutating one at runtime would silently split a body's data across two
    naming schemes, so the dataclass is frozen.
    """
    with pytest.raises(AttributeError):
        BODIES[0].slug = "tampered"  # type: ignore[misc]
