"""Naming tests, using identifiers actually observed on the site.

Every awkward value here is real, taken from the captured fixtures. They are the
reason this module exists: site identifiers are display strings, not keys.
"""

from __future__ import annotations

import pytest

from kedra_scraper.naming import curated_key, document_id, landing_key, slugify


def test_plain_identifier_is_simply_lowercased() -> None:
    assert slugify("ADJ-00047352") == "adj-00047352"


def test_spaces_around_separators_collapse_to_single_hyphens() -> None:
    """`IR - SC - 00001785` is a real WRC identifier.

    A naive space-to-hyphen replacement yields `ir---sc---00001785`, which is
    valid but ugly and, worse, not stable against the site changing its spacing.
    """
    assert slugify("IR - SC - 00001785") == "ir-sc-00001785"


def test_slashes_and_commas_cannot_leak_into_object_keys() -> None:
    """`RP2147/2009, MN1794/2009, WT796/2009` is a real EAT identifier.

    Used unescaped in an object key, its slashes create nested prefixes: the
    document would be filed under a directory named after part of its own
    identifier, and listing that prefix would not find it.
    """
    slug = slugify("RP2147/2009, MN1794/2009, WT796/2009")
    assert slug == "rp2147-2009-mn1794-2009-wt796-2009"
    assert "/" not in slug


def test_bare_numeric_identifiers_survive() -> None:
    """EAT identifiers are sometimes bare integers."""
    assert slugify("38086") == "38086"


def test_slug_never_starts_or_ends_with_a_hyphen() -> None:
    assert slugify("  --- ADJ-1 --- ") == "adj-1"


def test_an_identifier_with_no_usable_characters_is_rejected() -> None:
    """Better a loud failure than a document keyed on an empty string.

    An empty slug would make `_id` collapse to `body:` for every such record, so
    they would all upsert over one another and silently reduce to a single row.
    """
    with pytest.raises(ValueError, match="no usable characters"):
        slugify("///   ///")


def test_long_identifiers_are_bounded_and_still_well_formed() -> None:
    slug = slugify("A" * 400)
    assert len(slug) <= 120
    assert not slug.endswith("-")


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def test_document_id_is_scoped_by_body() -> None:
    """The reason keys are never the identifier alone.

    EAT identifiers are bare integers, so an unscoped key would let an EAT
    record and a Labour Court record with the same number upsert over each
    other -- losing one document with no error anywhere.
    """
    eat = document_id("employment_appeals_tribunal", "38086")
    labour = document_id("labour_court", "38086")
    assert eat != labour
    assert eat == "employment_appeals_tribunal:38086"


def test_document_id_is_deterministic() -> None:
    """This is what makes idempotency structural rather than conventional.

    Re-scraping produces the same `_id`, so the write is an upsert over the same
    document. Duplicates are impossible by construction, even if a run is
    interrupted halfway and repeated.
    """
    first = document_id("workplace_relations_commission", "ADJ-00047352")
    second = document_id("workplace_relations_commission", "ADJ-00047352")
    assert first == second == "workplace_relations_commission:adj-00047352"


def test_landing_key_preserves_the_original_filename() -> None:
    """The brief requires landing data stored as served, filename included."""
    key = landing_key(
        "employment_appeals_tribunal",
        "2010-12",
        "RP2147/2009, MN1794/2009",
        "75d3358e-f145-40d5-9922-da2822791892.pdf",
    )
    assert key == (
        "body=employment_appeals_tribunal/partition=2010-12/"
        "rp2147-2009-mn1794-2009/75d3358e-f145-40d5-9922-da2822791892.pdf"
    )


def test_curated_key_renames_to_the_identifier() -> None:
    """The transformation step's rename requirement."""
    key = curated_key("workplace_relations_commission", "2024-01", "ADJ-00047352", ".HTML")
    assert key == "body=workplace_relations_commission/partition=2024-01/adj-00047352.html"


def test_keys_are_partitioned_so_a_prefix_listing_is_cheap() -> None:
    """Body and partition are prefixes, not suffixes.

    Object stores only support prefix queries, so ordering the key this way is
    what makes "everything the WRC published in 2024-01" a single cheap listing
    rather than a full-bucket scan.
    """
    key = landing_key("labour_court", "2024-03", "LCR22912", "lcr22912.html")
    assert key.startswith("body=labour_court/partition=2024-03/")
