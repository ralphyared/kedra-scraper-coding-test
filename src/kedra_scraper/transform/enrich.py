"""Lifting a few structured fields out of the decision prose.

The brief invites extra steps in the transformation for data quality. This is
deliberately *light*: it pulls out the handful of fields the pages label
explicitly, and flags anomalies. It does not attempt to parse the reasoning, the
outcome, or the award, because those are written in free prose that varies by
decade and by adjudicator, and a confidently wrong extraction of an award figure
is far worse than no extraction at all.

Every field is optional. A missing value means "this page did not label it",
never "this decision lacks it" -- so downstream consumers must treat `None` as
unknown rather than as absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

# The pages label fields in bold, with the value as the following text node.
# Several variants exist because the wording differs between bodies and eras.
_HEARING_DATE_LABELS = ("date of hearing",)
# Matched as a substring, not a prefix: the WRC's label is the full
# "Workplace Relations Commission Adjudication Officer:".
#
# Generic terms like "chairman" and "employer member" were tried and removed.
# They matched unrelated bold text on Labour Court pages and returned "NOTE" as
# the adjudicator's name. A confidently wrong name is worse than an absent one,
# so this errs towards None and lets the quality flag record that nothing was
# found -- the Labour Court labels its bench "DIVISION:", which is a different
# concept and is deliberately not mapped onto this field.
_ADJUDICATOR_LABELS = ("adjudication officer",)
_REFERENCE_LABELS = (
    "investigation recommendation reference",
    "adjudication reference",
    "decision reference",
    "recommendation reference",
)

# Complaint references have a fixed, unambiguous shape, so a pattern is safer
# here than a label -- the label wording varies but this never does.
_COMPLAINT_REFERENCE = re.compile(r"\bCA-\d{6,}-\d{3}\b")
_ACT = re.compile(r"\b((?:[A-Z][A-Za-z]+ ){1,6}Act,? \d{4})\b")
_DDMMYYYY = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

# The site serves these itself, having mangled apostrophes and accents in its
# own publishing pipeline. They are preserved verbatim in the landing zone --
# it is what was served -- but the curated record should say so.
_REPLACEMENT_CHAR = "�"

# Below this a page is technically parseable but carries no decision worth
# indexing. Chosen from observation: real decisions run to hundreds of words,
# and the shortest genuine abstract seen was comfortably above this.
_STUB_WORD_COUNT = 40

# Field values here are names, dates and case references. Anything longer is
# body prose that happened to follow a label.
_MAX_FIELD_LENGTH = 120


@dataclass(frozen=True, slots=True)
class Enrichment:
    """Structured fields extracted from one decision, all optional."""

    hearing_date: date | None = None
    adjudicator: str | None = None
    legislation: str | None = None
    internal_reference: str | None = None
    complaint_references: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    representatives: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


def _labelled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """Find a bold label and return the text that follows it.

    Matched on a normalised prefix rather than equality, because the labels
    carry trailing colons and inconsistent spacing, and the same field is
    phrased differently across bodies.
    """
    for node in soup.find_all(["b", "strong", "th"]):
        text = " ".join(node.get_text(" ", strip=True).lower().split()).rstrip(":")
        # Substring, not prefix: the WRC prefixes its label with the body's own
        # name, so "adjudication officer" appears in the middle of it.
        if not any(label.rstrip(":") in text for label in labels):
            continue
        for following in node.find_all_next(string=True):
            # Skip the label's own text. `find_all_next` yields it first, and
            # comparing against the normalised `text` above does not exclude it
            # because that has been lowercased and stripped of its colon -- so
            # the colon check below would immediately reject the label itself
            # and report every field as absent.
            if following.find_parent(node.name) is node or following.parent is node:
                continue
            value = _norm(str(following))
            if not value:
                continue
            # A value that ends in a colon is the *next* label, which means this
            # field was empty. Returning it would attribute one field's name to
            # another field's value -- the mistake that produced "NOTE" as an
            # adjudicator.
            if value.endswith(":"):
                return None
            # Field values on these pages are names, dates and references. A
            # long string means the label was followed by body prose rather than
            # a value, so it is rejected rather than truncated.
            if len(value) > _MAX_FIELD_LENGTH:
                return None
            return value
    return None


def _norm(value: str) -> str:
    """Collapse all whitespace, including the non-breaking spaces the site pads with.

    `str.split()` already treats U+00A0 as whitespace, so this is a join over
    split rather than a regex -- but it has to be applied everywhere a value is
    read, or names arrive padded with dozens of them.
    """
    return " ".join(value.split())


def _table_column(soup: BeautifulSoup, header: str) -> tuple[str, ...]:
    """Collect the cells of the table row whose first cell matches `header`."""
    for row in soup.find_all("tr"):
        cells = [_norm(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        if not cells:
            continue
        if cells[0].lower().startswith(header.lower()):
            return tuple(c for c in cells[1:] if c)
    return ()


def enrich(cleaned_html: str, text: str) -> Enrichment:
    """Extract what the page labels, and flag what looks wrong.

    Takes the *cleaned* html rather than the raw page: the navigation and footer
    contain dates and proper nouns of their own, and matching against them would
    attribute the site's copyright year to a decision.
    """
    soup = BeautifulSoup(cleaned_html, "lxml")

    hearing_date = None
    raw_hearing = _labelled_value(soup, _HEARING_DATE_LABELS)
    if raw_hearing and (match := _DDMMYYYY.search(raw_hearing)):
        day, month, year = (int(g) for g in match.groups())
        try:
            hearing_date = date(year, month, day)
        except ValueError:
            hearing_date = None  # an impossible date is not worth guessing at

    act_match = _ACT.search(text)

    parties = _table_column(soup, "anonymised parties") or _table_column(soup, "parties")
    representatives = _table_column(soup, "representatives")

    flags: list[str] = []
    words = len(text.split())
    if words == 0:
        flags.append("empty_content")
    elif words < _STUB_WORD_COUNT:
        flags.append("stub_content")
    if _REPLACEMENT_CHAR in text:
        # Recorded, not repaired. The mangling happened upstream, in the site's
        # own publishing; "fixing" it here would be inventing characters that
        # were never served.
        flags.append("source_encoding_damage")
    # Deliberately NOT flagged: a missing hearing date or parties table.
    #
    # The first version did flag those, and every Labour Court record came back
    # flagged -- that body labels its bench "DIVISION:" and does not publish a
    # parties table at all. The flag was therefore describing a structural
    # property of the body rather than a problem with the document, which makes
    # "flagged" useless as a signal: a reviewer filtering on it would be handed
    # every Labour Court decision ever issued.
    #
    # A field that is None already says it was not found. Quality flags are
    # reserved for cases where the *document itself* is wrong.

    return Enrichment(
        hearing_date=hearing_date,
        adjudicator=_labelled_value(soup, _ADJUDICATOR_LABELS),
        legislation=act_match.group(1) if act_match else None,
        internal_reference=_labelled_value(soup, _REFERENCE_LABELS),
        complaint_references=tuple(dict.fromkeys(_COMPLAINT_REFERENCE.findall(text))),
        parties=parties,
        representatives=representatives,
        quality_flags=tuple(flags),
    )
