"""Shared helpers for the offline test suite.

Every test in this suite runs against captured fixtures with no network access.
That is a deliberate property, not a convenience: parser behaviour must be
reproducible and must not depend on the live site being up, unchanged, or
reachable from wherever the tests happen to run.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    """Read a captured page as text.

    No `errors=` argument, deliberately. The captured bytes are valid UTF-8, and
    if that ever stops being true the tests should fail loudly rather than
    quietly substituting replacement characters and testing something else.

    Note that the pages legitimately *contain* U+FFFD -- the site itself serves
    mangled apostrophes -- which is different from the bytes being undecodable.
    """
    return (FIXTURES / name).read_text(encoding="utf-8")


def read_fixture_bytes(name: str) -> bytes:
    """Read a captured page as raw bytes, for hashing tests."""
    return (FIXTURES / name).read_bytes()
