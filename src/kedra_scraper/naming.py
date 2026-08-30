"""Turning site identifiers into safe, stable keys.

Identifiers on this site are display strings, not keys. They contain spaces
(`IR - SC - 00001785`), path separators and commas (`RP2147/2009, MN1794/2009,
WT796/2009`), and for the EAT they are bare integers (`38086`) that carry no
information about which body issued them.

Two consequences drive this module:

* A raw identifier cannot be used in an object storage key. `RP2147/2009` would
  silently create a nested prefix, so one logical document would be filed under
  a directory named after part of its own name.
* A raw identifier cannot be used as a primary key either, because a bare EAT
  integer could collide with an id from another body. Keys are therefore always
  scoped by body.

The raw value is never discarded -- it is preserved verbatim in the metadata
record. This module only produces the derived, safe form.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Long enough for every identifier observed on the site, short enough to stay
# well inside object-key and filesystem limits once prefixes are added.
_MAX_SLUG_LEN = 120


def slugify(value: str) -> str:
    """Lowercase `value` and reduce it to `[a-z0-9-]`.

    Runs of unsafe characters collapse to a single hyphen, so
    `RP2147/2009, MN1794/2009` becomes `rp2147-2009-mn1794-2009` rather than
    growing a hyphen per punctuation mark.
    """
    slug = _NON_ALNUM.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"identifier {value!r} contains no usable characters")
    return slug[:_MAX_SLUG_LEN].rstrip("-")


def document_id(body_slug: str, identifier: str) -> str:
    """Deterministic primary key for one decision: `<body>:<identifier-slug>`.

    Determinism is what makes the pipeline idempotent by construction rather
    than by convention: re-scraping the same decision produces the same `_id`,
    so the write is an upsert over the same document and duplicates are
    impossible even if a run is interrupted and repeated.
    """
    return f"{body_slug}:{slugify(identifier)}"


def versioned_filename(filename: str, version: str) -> str:
    """Insert a version marker before the extension: `a.pdf` -> `a__abc123.pdf`.

    Before the extension rather than after, so the file still reads as a PDF to
    anything that dispatches on extension -- including a reviewer browsing the
    MinIO console.
    """
    stem, dot, extension = filename.rpartition(".")
    if not dot:
        return f"{filename}__{version}"
    return f"{stem}__{version}.{extension}"


def landing_key(
    body_slug: str, partition: str, identifier: str, filename: str, version: str = ""
) -> str:
    """Object key in the landing bucket.

    The original filename is preserved: the brief requires landing-zone data to
    be stored as served, and the name the site chose is part of that.

    `version` exists because the landing zone is append-only. The brief forbids
    deleting or updating stored data, so when a decision is re-published with
    different content the new bytes go to a new key rather than over the old
    ones. The first copy keeps the clean name; only subsequent versions carry a
    marker, so the common case stays readable and history is still preserved.
    """
    name = versioned_filename(filename, version) if version else filename
    return f"body={body_slug}/partition={partition}/{slugify(identifier)}/{name}"


def curated_key(body_slug: str, partition: str, identifier: str, extension: str) -> str:
    """Object key in the curated bucket, renamed to `<identifier>.<ext>` per spec."""
    ext = extension.lstrip(".").lower()
    return f"body={body_slug}/partition={partition}/{slugify(identifier)}.{ext}"
