"""Tests for the transformation, run against the real captured pages.

The cleaning rules decide what a reader of the curated zone will ever see, so
the tests assert both directions: that the furniture is gone, and that the
decision survived. A cleaner that removed everything would pass a test that only
checked for the absence of navigation.
"""

from __future__ import annotations

from datetime import date

from conftest import read_fixture
from kedra_scraper.transform.cleaner import clean_html
from kedra_scraper.transform.enrich import enrich
from kedra_scraper.transform.run import _extension

WRC = "case_wrc_inline.html"
LABOUR_COURT = "case_labour_court_inline.html"
EAT = "case_eat_pdf_only.html"
EQUALITY = "case_equality_tribunal_html_plus_pdf.html"


def _clean(name: str):  # type: ignore[no-untyped-def]
    return clean_html(read_fixture(name))


# --------------------------------------------------------------------------
# Cleaning: what must go
# --------------------------------------------------------------------------


def test_page_furniture_is_removed() -> None:
    """The brief's transformation step asks for exactly this."""
    html = _clean(WRC).html.lower()
    for furniture in ("<nav", "<footer", "<header", "<script", "cookie"):
        assert furniture not in html, f"{furniture} survived cleaning"


def test_event_handlers_and_inline_styles_are_stripped() -> None:
    """An `onclick` in a stored artifact is a liability for no benefit."""
    html = _clean(WRC).html.lower()
    assert "onclick" not in html
    assert "style=" not in html


def test_scripts_inside_the_content_region_are_removed_too() -> None:
    """The whitelist keeps `div.content`, so anything inside it is kept by
    default -- scripts have to be removed explicitly or a tracking pixel in the
    body would be carried into the curated zone."""
    raw = """
    <html><body>
      <h1 class="page-title">ADJ-1</h1>
      <div class="content"><p>Decision text.</p><script>evil()</script></div>
    </body></html>"""
    cleaned = clean_html(raw)
    assert "evil" not in cleaned.html
    assert "Decision text." in cleaned.text


# --------------------------------------------------------------------------
# Cleaning: what must stay
# --------------------------------------------------------------------------


def test_the_decision_itself_survives() -> None:
    """The other half of the guarantee.

    A cleaner that emptied the document would satisfy every "furniture is gone"
    assertion above, so the content is checked explicitly.
    """
    cleaned = _clean(WRC)
    assert cleaned.word_count > 1500
    assert "Adjudication Officer" in cleaned.text
    assert cleaned.title == "ADJ-00047352"


def test_document_structure_is_preserved() -> None:
    """Tables of parties and headings carry meaning; flattening loses it."""
    html = _clean(WRC).html
    assert "<table" in html
    assert "<h1" in html


def test_non_breaking_space_padding_is_collapsed() -> None:
    """The site pads names with runs of U+00A0 for visual alignment.

    Left in place they are invisible on screen but defeat any exact match on a
    representative's name.
    """
    assert "\u00a0" not in _clean(WRC).text


def test_a_page_with_no_content_region_does_not_crash() -> None:
    """Cleaning must degrade to empty, not raise. An EAT page's content div is
    present but empty, and a transformation that threw would stall the whole
    partition over a document that is legitimately blank."""
    cleaned = _clean(EAT)
    assert cleaned.word_count <= 5


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------


def test_labelled_fields_are_extracted_from_a_wrc_decision() -> None:
    cleaned = _clean(WRC)
    result = enrich(cleaned.html, cleaned.text)
    assert result.hearing_date == date(2024, 1, 16)
    assert result.adjudicator == "Penelope McGrath"
    assert result.legislation == "Industrial Relations Act 1969"
    assert result.internal_reference == "ADJ 47352"
    assert result.complaint_references == ("CA-00058221-001",)
    assert result.parties == ("Car Valet", "Motor Garage")


def test_representative_names_are_not_padded() -> None:
    cleaned = _clean(WRC)
    result = enrich(cleaned.html, cleaned.text)
    assert result.representatives == ("Derek Murphy, Derek Murphy Solicitors",)


def test_an_unlabelled_field_returns_none_rather_than_a_guess() -> None:
    """Regression: the first version returned "NOTE" as a Labour Court adjudicator.

    A generic `chairman` pattern matched unrelated bold text and the extractor
    took whatever followed. A confidently wrong name is far worse than an absent
    one -- it is indistinguishable from a correct one downstream.
    """
    cleaned = _clean(LABOUR_COURT)
    result = enrich(cleaned.html, cleaned.text)
    assert result.adjudicator is None
    assert result.hearing_date is None


def test_a_body_that_labels_nothing_is_not_flagged_as_defective() -> None:
    """Regression: every Labour Court record came back flagged.

    Absent optional fields were treated as quality problems, so `flagged`
    described a structural property of the body rather than a defect, and a
    reviewer filtering on it would be handed every Labour Court decision ever
    issued. Missing fields are conveyed by their being None.
    """
    cleaned = _clean(LABOUR_COURT)
    result = enrich(cleaned.html, cleaned.text)
    assert result.quality_flags == ()


def test_genuinely_defective_documents_are_flagged() -> None:
    """The flags that remain must still fire when something is really wrong."""
    empty = enrich("<article></article>", "")
    assert "empty_content" in empty.quality_flags

    stub = enrich("<article><p>x</p></article>", "a few words only")
    assert "stub_content" in stub.quality_flags


def test_source_encoding_damage_is_recorded_not_repaired() -> None:
    """The site serves U+FFFD itself, having mangled text upstream.

    Repairing it here would invent characters that were never published, so the
    curated record notes the damage and leaves the text alone.
    """
    damaged = enrich("<article><p>x</p></article>", "St. Vincent" + chr(0xFFFD) + "s Hospital")
    assert "source_encoding_damage" in damaged.quality_flags


def test_dates_are_read_day_first() -> None:
    """16/01/2024 is 16 January. Read month-first it is invalid, but an
    ambiguous date like 05/06 would be silently misfiled by five months."""
    cleaned = _clean(WRC)
    assert enrich(cleaned.html, cleaned.text).hearing_date == date(2024, 1, 16)


def test_an_abstract_still_yields_its_legislation() -> None:
    """Equality Tribunal pages carry only a short abstract, but it names the Act."""
    cleaned = _clean(EQUALITY)
    assert enrich(cleaned.html, cleaned.text).legislation == "Employment Equality Act, 1998"


# --------------------------------------------------------------------------
# Curated naming
# --------------------------------------------------------------------------


def test_extension_comes_from_the_stored_filename() -> None:
    assert _extension("adj-00047352.html", "text/html; charset=utf-8") == "html"
    assert _extension("75d3358e-f145.pdf", "application/pdf") == "pdf"


def test_extension_falls_back_to_content_type_when_the_name_has_none() -> None:
    """Some EAT attachments are named with a bare UUID and no suffix."""
    assert _extension("75d3358e-f145-40d5-9922", "application/pdf") == "bin"
    assert _extension("index", "text/html; charset=utf-8") == "html"


def test_a_uuid_filename_is_not_mistaken_for_an_extension() -> None:
    """`rpartition('.')` on a dotted UUID would otherwise yield a long tail as
    the extension, producing keys like `adj-1.40d5-9922-da2822791892`."""
    assert _extension("75d3358e-f145-40d5-9922-da2822791892", "application/pdf") == "bin"
