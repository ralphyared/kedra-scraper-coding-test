"""Registry of the four decision-issuing bodies on workplacerelations.ie.

The numeric ids are the site's own `body=` query parameter values, read off the
search form's checkboxes (see tests/fixtures/search_form_excerpt.html). They are
not sequential and not guessable -- the WRC is 15376 -- so they are recorded here
rather than derived.

One body per request, always. A multi-valued `body=15376&body=3` does not
intersect the two; it silently returns the same result set as no filter at all,
which would look like a successful crawl while quietly scraping everything.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Body:
    """One decision-issuing body.

    `slug` is the stable internal name. It appears in Mongo `_id` values and in
    object storage keys, so it must never change once data exists.
    """

    id: int
    slug: str
    label: str
    aliases: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.slug, *self.aliases)


# Total records per body, measured 2026-08-28. Recorded to justify the
# partition-size reasoning in ARCHITECTURE.md; nothing in the code reads them.
#
#     Equality Tribunal              ~3,170
#     Employment Appeals Tribunal   ~16,527
#     Labour Court                  ~19,040
#     Workplace Relations Commission ~23,935
#                                   -------
#                                   ~62,672
BODIES: tuple[Body, ...] = (
    Body(1, "equality_tribunal", "Equality Tribunal", ("et",)),
    Body(2, "employment_appeals_tribunal", "Employment Appeals Tribunal", ("eat",)),
    Body(3, "labour_court", "Labour Court", ("lc",)),
    Body(
        15376,
        "workplace_relations_commission",
        "Workplace Relations Commission",
        ("wrc",),
    ),
)

_BY_NAME: dict[str, Body] = {name: b for b in BODIES for name in b.all_names}
_BY_ID: dict[int, Body] = {b.id: b for b in BODIES}


def resolve(token: str) -> Body:
    """Look up a body by slug or short alias, case-insensitively.

    Raises ValueError listing the valid names, because this is reached straight
    from a command-line argument and a silent fallback to "all bodies" would be
    the worst possible failure mode.
    """
    key = token.strip().lower()
    try:
        return _BY_NAME[key]
    except KeyError:
        raise ValueError(f"unknown body {token!r}; expected one of {sorted(_BY_NAME)}") from None


def by_id(body_id: int) -> Body:
    """Look up a body by the site's numeric id."""
    try:
        return _BY_ID[body_id]
    except KeyError:
        raise ValueError(f"unknown body id {body_id}") from None
