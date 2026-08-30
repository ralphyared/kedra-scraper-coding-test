"""Two hashes per document, and the reason there have to be two.

Every case page on this site embeds a per-request server timing comment:

    <!-- Elapsed time: 0.1249982 -->

The value changes on every request. Two fetches of a page that has not been
edited therefore produce different bytes and different `sha256`. A naive hash
over raw bytes makes *every* record look changed on *every* run, which silently
destroys the idempotency requirement: the pipeline would re-download and
re-write all 62k documents each time while reporting that it had correctly
detected changes.

So each document carries two hashes:

`file_hash`
    sha256 of exactly the bytes stored in the landing zone. The brief asks for a
    hash of the file, and this is it -- verifiable by anyone who downloads the
    object and hashes it themselves.

`content_hash`
    sha256 over a normalised form with volatile markup removed. This is what
    change detection and deduplication actually compare, because it answers the
    question we care about: did the *document* change, not did the *response*.

Normalisation only ever removes things that cannot carry document meaning:
HTML comments, which are never rendered, and runs of whitespace, which HTML
already collapses when displayed. Anything a reader could see survives it.
"""

from __future__ import annotations

import hashlib
import re

HASH_PREFIX = "sha256:"

# Non-greedy and DOTALL: comments legitimately span lines, and a greedy match
# would swallow the entire document between the first `<!--` and the last `-->`.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def sha256_bytes(data: bytes) -> str:
    """Return `sha256:<hex>` for `data`.

    The algorithm is carried in the value rather than assumed. If this ever
    moves to a different digest, existing records remain self-describing instead
    of becoming ambiguous strings of the wrong length.
    """
    return f"{HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def normalise_html(html: str) -> str:
    """Strip volatile markup so two fetches of an unchanged page compare equal.

    Comments go first: dropping them removes the `Elapsed time` timestamp, and
    doing it generically means a second volatile comment appearing later needs
    no code change. Whitespace is then collapsed, which absorbs the reflowing
    that comment removal leaves behind.
    """
    return _WHITESPACE.sub(" ", _COMMENT.sub("", html)).strip()


def file_hash(data: bytes) -> str:
    """Hash of the exact stored bytes. Never normalised."""
    return sha256_bytes(data)


def content_hash(data: bytes, *, is_html: bool) -> str:
    """Hash used for change detection and deduplication.

    Binary documents (PDF, DOC) have no volatile wrapper to remove, so their
    content hash is simply their file hash. Keeping one entry point rather than
    making callers branch means a caller cannot forget to normalise HTML.

    Decoding is deliberately lossy (`errors="replace"`). This input is untrusted
    third-party markup with an occasionally mislabelled encoding, and a
    `UnicodeDecodeError` here would abort a partition over a single stray byte.
    Replacement is deterministic, so the same bytes still yield the same hash.
    """
    if not is_html:
        return sha256_bytes(data)
    return sha256_bytes(normalise_html(data.decode("utf-8", errors="replace")).encode("utf-8"))
