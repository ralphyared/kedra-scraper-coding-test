"""Lifting a few structured fields out of the decision prose.

The brief invites extra steps in the transformation for data quality. This is
deliberately *light*: it pulls out the handful of fields the pages identify, and
flags anomalies. It does not attempt to parse the reasoning, the outcome, or the
award, because those are free prose that varies by decade and by adjudicator,
and a confidently wrong award figure is far worse than no award figure.

Two page layouts are handled, because two of the four bodies genuinely differ:

* The **WRC** labels fields in bold and tabulates its parties.
* The **Labour Court** writes its parties as a sentence, sits as a three-person
  division rather than a single adjudicator, and states the hearing date in
  narrative prose in a different date format.

Extraction is by layout, not by body slug. A page is tried against both, and
whichever yields data wins -- so a page that does not follow its body's usual
shape still gets read, and adding a fifth body means adding a strategy rather
than editing a conditional.

Every field is optional. A missing value means "this page did not state it",
never "this decision lacks it" -- so `None` must be read as unknown, not absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

# --- WRC layout -----------------------------------------------------------
# Fields are bold labels with the value in the following text node.
# Both wordings are in live use, and the second is not a superstring of the
# first -- "date of hearing" does not occur inside "date of adjudication
# hearing", because the words are interleaved. Listing only the shorter one
# found the date on 115 of 766 WRC records and left the rest looking as though
# the site had simply not stated it.
_HEARING_DATE_LABELS = ("date of hearing", "date of adjudication hearing")
# Matched as a substring, not a prefix: the WRC's label is the full
# "Workplace Relations Commission Adjudication Officer:".
#
# Generic terms like "chairman" were tried and removed. They matched unrelated
# bold text on Labour Court pages and returned "NOTE" as the adjudicator's name.
# A confidently wrong name is worse than an absent one, so this stays narrow and
# the Labour Court's bench is read by the division strategy below instead.
_ADJUDICATOR_LABELS = ("adjudication officer",)
_REFERENCE_LABELS = (
    "investigation recommendation reference",
    "adjudication reference",
    "decision reference",
    "recommendation reference",
)

# --- Labour Court layout --------------------------------------------------
# "PARTIES: SONOMA VALLEY (REPRESENTED BY ...) AND A WORKER"
_LC_PARTY_SPLIT = re.compile(r"\bAND\b")
_LC_REPRESENTED = re.compile(r"\(\s*REPRESENTED BY\s+(.+?)\s*\)", re.I | re.S)
# "Chairman: Mr Foley", "Employer Member: Ms Doyle", "Worker Member: Mr Bell"
_LC_BENCH_ROLE = re.compile(
    r"\b(Chairman|Deputy Chairman|Employer Member|Worker Member)\s*:\s*(.+)", re.I
)
# The Labour Court states its hearing in a sentence rather than a labelled field.
_LC_HEARING_SENTENCE = re.compile(
    r"hearing (?:took place|was held)(?:\s+\w+){0,3}?\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", re.I
)
_LC_CASE_REFERENCE = re.compile(r"\b([A-Z]{2,4}/\d{2}/\d{1,4})\b")

# The Labour Court sits as a division of three. Listed explicitly so an
# unrecognised role is dropped rather than paired with the wrong name.
_BENCH_ROLES = frozenset({"chairman", "deputy chairman", "employer member", "worker member"})

# --- shared ---------------------------------------------------------------
# Complaint references have a fixed, unambiguous shape, so a pattern is safer
# here than a label: the label wording varies but this never does.
_COMPLAINT_REFERENCE = re.compile(r"\bCA-\d{6,}-\d{3}\b")
_ACT = re.compile(r"\b((?:[A-Z][A-Za-z]+ ){1,6}Act,? \d{4})\b")
_DDMMYYYY = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_PROSE_DATE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b")

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}

# The site serves these itself, having mangled text in its own publishing.
# Preserved verbatim in the landing zone -- it is what was served -- but the
# curated record should say so.
_REPLACEMENT_CHAR = "�"

# Below this a page is parseable but carries no decision worth indexing. Chosen
# from observation: the shortest genuine abstract seen was comfortably above it.
_STUB_WORD_COUNT = 40

# Field values are names, dates and references. Anything longer is body prose
# that happened to follow a label.
_MAX_FIELD_LENGTH = 120


@dataclass(frozen=True, slots=True)
class Enrichment:
    """Structured fields extracted from one decision, all optional."""

    hearing_date: date | None = None
    adjudicator: str | None = None
    division: tuple[str, ...] = ()
    legislation: str | None = None
    internal_reference: str | None = None
    complaint_references: tuple[str, ...] = ()
    parties: tuple[str, ...] = ()
    representatives: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decided_by(self) -> tuple[str, ...]:
        """Whoever decided the case, whatever the body calls them.

        The WRC appoints a single adjudication officer; the Labour Court sits as
        a division of three. They are different institutions with different
        structures, so the two are stored separately rather than flattened --
        but a consumer asking "who decided this?" should not have to know that.
        """
        if self.adjudicator:
            return (self.adjudicator,)
        return self.division


def _norm(value: str) -> str:
    """Collapse all whitespace, including the non-breaking spaces the site pads with."""
    return " ".join(value.split())


def _parse_prose_date(value: str) -> date | None:
    """Parse `5 January 2024`, the form the Labour Court writes in prose."""
    match = _PROSE_DATE.search(value)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _parse_ddmmyyyy(value: str) -> date | None:
    """Parse `16/01/2024`, the form the WRC uses in its labelled field."""
    match = _DDMMYYYY.search(value)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None  # an impossible date is not worth guessing at


def _labelled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """Find a bold label and return the text that follows it."""
    for node in soup.find_all(["b", "strong", "th"]):
        text = _norm(node.get_text(" ", strip=True).lower()).rstrip(":")
        if not any(label.rstrip(":") in text for label in labels):
            continue
        for following in node.find_all_next(string=True):
            # Skip the label's own text; `find_all_next` yields it first.
            if following.find_parent(node.name) is node or following.parent is node:
                continue
            value = _norm(str(following))
            if not value:
                continue
            # A value ending in a colon is the *next* label, meaning this field
            # was empty. Returning it would attribute one field's name to
            # another's value -- the mistake that produced "NOTE" as adjudicator.
            if value.endswith(":") or len(value) > _MAX_FIELD_LENGTH:
                return None
            return value
    return None


def _table_column(soup: BeautifulSoup, header: str) -> tuple[str, ...]:
    """Collect the cells of the table row whose first cell matches `header`."""
    for row in soup.find_all("tr"):
        cells = [_norm(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        if cells and cells[0].lower().startswith(header.lower()):
            return tuple(c for c in cells[1:] if c)
    return ()


def _section(text: str, start: str, *ends: str) -> str:
    """Return the text between one heading and the next.

    The Labour Court's fields are sections of prose rather than labelled values,
    so they are delimited by the heading that follows them.
    """
    lowered = text.lower()
    begin = lowered.find(start.lower())
    if begin == -1:
        return ""
    begin += len(start)
    stop = len(text)
    for end in ends:
        found = lowered.find(end.lower(), begin)
        if found != -1:
            stop = min(stop, found)
    return text[begin:stop].strip()


def _labour_court_parties(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the Labour Court's parties sentence.

    "SONOMA VALLEY (REPRESENTED BY ANNE O'CONNELL, SOLICITOR) AND A WORKER"
    yields two parties and one representative. The representation is stripped
    from the party name rather than left attached, so that "SONOMA VALLEY" is
    comparable against the same employer appearing in another case.
    """
    section = _section(text, "PARTIES:", "DIVISION:", "SUBJECT", "BACKGROUND:")
    if not section:
        return (), ()

    representatives = tuple(_norm(m) for m in _LC_REPRESENTED.findall(section))
    without = _LC_REPRESENTED.sub(" ", section)
    parties = tuple(p for p in (_norm(part) for part in _LC_PARTY_SPLIT.split(without)) if p)
    return parties, representatives


def _labour_court_division(text: str) -> tuple[str, ...]:
    """Read the three-person bench, as `Role: Name` pairs."""
    section = _section(text, "DIVISION:", "SUBJECT", "BACKGROUND:")
    if not section:
        return ()
    members = []
    for line in section.splitlines():
        match = _LC_BENCH_ROLE.match(_norm(line))
        if match:
            members.append(f"{_norm(match.group(1))}: {_norm(match.group(2))}")
    if members:
        return tuple(members)
    # Roles and names alternate across lines on some pages, so fall back to
    # pairing them up rather than returning nothing.
    lines = [_norm(x) for x in section.splitlines() if _norm(x)]
    return tuple(
        f"{role.rstrip(':')}: {name}"
        for role, name in zip(lines[::2], lines[1::2], strict=False)
        if role.rstrip(":").lower() in _BENCH_ROLES
    )


def enrich(cleaned_html: str, text: str) -> Enrichment:
    """Extract what the page states, and flag what looks wrong.

    Takes the *cleaned* html rather than the raw page: the navigation and footer
    contain dates and proper nouns of their own, and matching against them would
    attribute the site's copyright year to a decision.
    """
    soup = BeautifulSoup(cleaned_html, "lxml")

    # --- WRC layout: bold labels and a parties table ---------------------
    hearing_raw = _labelled_value(soup, _HEARING_DATE_LABELS)
    hearing_date = _parse_ddmmyyyy(hearing_raw) if hearing_raw else None
    adjudicator = _labelled_value(soup, _ADJUDICATOR_LABELS)
    internal_reference = _labelled_value(soup, _REFERENCE_LABELS)
    parties = _table_column(soup, "anonymised parties") or _table_column(soup, "parties")
    representatives = _table_column(soup, "representatives")

    # --- Labour Court layout: prose sections ------------------------------
    # Applied only where the WRC layout found nothing, so a page carrying both
    # shapes keeps the labelled values, which are the more precise of the two.
    division = _labour_court_division(text)
    if not parties:
        parties, lc_representatives = _labour_court_parties(text)
        representatives = representatives or lc_representatives
    if hearing_date is None and (match := _LC_HEARING_SENTENCE.search(text)):
        hearing_date = _parse_prose_date(match.group(1))
    if internal_reference is None and (match := _LC_CASE_REFERENCE.search(text)):
        internal_reference = match.group(1)

    act_match = _ACT.search(text)

    flags: list[str] = []
    words = len(text.split())
    if words == 0:
        flags.append("empty_content")
    elif words < _STUB_WORD_COUNT:
        flags.append("stub_content")
    if _REPLACEMENT_CHAR in text:
        # Recorded, not repaired. The mangling happened upstream in the site's
        # own publishing; "fixing" it would invent characters never served.
        flags.append("source_encoding_damage")

    # Deliberately NOT flagged: a missing hearing date or parties table.
    #
    # An earlier version did, and every Labour Court record came back flagged --
    # the flag described a structural property of the body rather than a problem
    # with the document, which makes "flagged" useless as a signal. A field that
    # is None already says it was not found; quality flags are reserved for the
    # document itself being wrong.

    return Enrichment(
        hearing_date=hearing_date,
        adjudicator=adjudicator,
        division=division,
        legislation=act_match.group(1) if act_match else None,
        internal_reference=internal_reference,
        complaint_references=tuple(dict.fromkeys(_COMPLAINT_REFERENCE.findall(text))),
        parties=parties,
        representatives=representatives,
        quality_flags=tuple(flags),
    )
