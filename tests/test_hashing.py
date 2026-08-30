"""Hashing tests, anchored on a real pair of fetches of the same page.

The central claim of this module -- that raw byte hashes are unusable for change
detection on this site -- is not asserted, it is demonstrated against two
captured responses for the same unchanged URL.
"""

from __future__ import annotations

from conftest import read_fixture_bytes
from kedra_scraper.hashing import (
    HASH_PREFIX,
    content_hash,
    file_hash,
    normalise_html,
    sha256_bytes,
)

ORIGINAL = "case_wrc_inline.html"
REFETCH = "case_wrc_inline__refetch.html"


def test_the_two_fetches_really_are_different_bytes() -> None:
    """Guards the premise of every test below.

    If someone ever "tidied" the fixtures by copying one over the other, the
    idempotency test would pass trivially and stop proving anything.
    """
    assert read_fixture_bytes(ORIGINAL) != read_fixture_bytes(REFETCH)


def test_raw_file_hashes_differ_between_two_fetches_of_an_unchanged_page() -> None:
    """This is the trap the whole design exists to avoid.

    The page was not edited between these two responses. Only the embedded
    `<!-- Elapsed time: N -->` server timing comment changed. Keying change
    detection on this hash would mark all 62k documents as modified on every
    run, re-downloading and re-writing the entire corpus while reporting that
    change detection was working.
    """
    assert file_hash(read_fixture_bytes(ORIGINAL)) != file_hash(read_fixture_bytes(REFETCH))


def test_content_hashes_are_equal_for_two_fetches_of_an_unchanged_page() -> None:
    """The property that makes re-runs idempotent."""
    original = content_hash(read_fixture_bytes(ORIGINAL), is_html=True)
    refetch = content_hash(read_fixture_bytes(REFETCH), is_html=True)
    assert original == refetch


def test_content_hash_still_detects_a_genuine_change() -> None:
    """Normalisation must not be so aggressive that real edits vanish.

    A hash that ignores everything is stable but useless; this is the other half
    of the guarantee, and the reason normalisation only removes comments and
    whitespace rather than, say, all markup.
    """
    original = read_fixture_bytes(ORIGINAL)
    edited = original.replace(b"Car Valet", b"Bicycle Valet")
    assert edited != original
    assert content_hash(edited, is_html=True) != content_hash(original, is_html=True)


def test_normalise_removes_the_volatile_timing_comment() -> None:
    a = normalise_html("<p>x</p><!-- Elapsed time: 0.0781157 -->")
    b = normalise_html("<p>x</p><!-- Elapsed time: 0.0312535 -->")
    assert a == b == "<p>x</p>"


def test_normalise_handles_comments_spanning_multiple_lines() -> None:
    """The comment regex is non-greedy and DOTALL.

    Greedy matching would delete everything between the first `<!--` and the
    last `-->` on the page -- which, on a page with two comments, silently
    discards all the content between them.
    """
    html = "<p>keep me</p><!-- one -->middle<!--\nspans\nlines\n-->tail"
    # `middle` and `tail` join, because a comment is not a word boundary -- this
    # is exactly how a browser renders it. What matters is that the text either
    # side of both comments survives rather than being swallowed wholesale.
    assert normalise_html(html) == "<p>keep me</p>middletail"


def test_normalise_collapses_whitespace() -> None:
    assert normalise_html("<p>a</p>\n\n   \t<p>b</p>") == "<p>a</p> <p>b</p>"


def test_binary_content_hash_equals_its_file_hash() -> None:
    """PDFs have no volatile wrapper, so there is nothing to normalise.

    Routing them through the same entry point means a caller cannot forget to
    normalise HTML, without pretending a PDF needs it.
    """
    data = b"%PDF-1.4 not really a pdf"
    assert content_hash(data, is_html=False) == file_hash(data)


def test_normalising_a_pdf_as_html_would_corrupt_it() -> None:
    """Why `is_html` is an explicit, required keyword rather than sniffed.

    PDF bytes contain whitespace that is structurally significant. Collapsing it
    changes the hash of a file that never changed, reintroducing exactly the
    problem this module exists to solve -- so the caller must state which it has.
    """
    pdf_like = b"%PDF-1.4\nstream\n   \n   binary   spacing\nendstream"
    assert content_hash(pdf_like, is_html=False) != content_hash(pdf_like, is_html=True)


def test_hash_values_carry_their_algorithm() -> None:
    """Self-describing, so a future digest change does not create ambiguity."""
    value = sha256_bytes(b"abc")
    assert value.startswith(HASH_PREFIX)
    assert len(value) == len(HASH_PREFIX) + 64


def test_hashing_is_deterministic_across_calls() -> None:
    assert sha256_bytes(b"same input") == sha256_bytes(b"same input")


def test_lossy_decoding_is_still_deterministic() -> None:
    """Undecodable bytes must not make a document's hash unstable.

    The site serves occasional invalid sequences. Replacement is deterministic,
    so the same bytes always produce the same hash rather than the document
    appearing to change on every fetch.
    """
    invalid = b"<p>caf\xe9 unpaired surrogate \xed\xa0\x80</p>"
    assert content_hash(invalid, is_html=True) == content_hash(invalid, is_html=True)
