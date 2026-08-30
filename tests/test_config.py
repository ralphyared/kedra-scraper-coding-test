"""Unit tests for settings construction.

These build the settings objects with explicit keyword arguments rather than by
patching the environment. The environment path is exercised every time anything
runs; what deserves a test is the assembly logic, and passing values directly
makes each case deterministic and independent of whatever `.env` happens to hold.
"""

from __future__ import annotations

from kedra_scraper.config import MongoSettings


def _mongo(**overrides: object) -> MongoSettings:
    defaults: dict[str, object] = {
        "username": "kedra",
        "password": "secret",
        "host": "localhost",
        "port": 27017,
        "auth_source": "admin",
        "db": "wrc",
    }
    return MongoSettings(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_uri_is_assembled_from_its_parts() -> None:
    assert _mongo().uri == "mongodb://kedra:secret@localhost:27017/?authSource=admin"


def test_uri_tracks_a_changed_port() -> None:
    """The host port is configurable, so the URI must follow it.

    This is the regression that motivated building the URI instead of storing it:
    a machine with its own MongoDB on 27017 maps the container elsewhere, and a
    hardcoded connection string would silently keep pointing at the wrong server.
    """
    assert _mongo(port=27018).uri.endswith("@localhost:27018/?authSource=admin")


def test_credentials_with_reserved_characters_are_escaped() -> None:
    """A password containing URI-reserved characters must not break the URI.

    Unescaped, `p@ss:w/rd?` would terminate the userinfo section at the first `@`
    and leave pymongo parsing `ss:w/rd?` as the host -- a connection to somewhere
    entirely unintended rather than a clean failure.
    """
    uri = _mongo(username="user@corp", password="p@ss:w/rd?").uri
    assert "user%40corp" in uri
    assert "p%40ss%3Aw%2Frd%3F" in uri
    # Exactly one `@` may remain: the delimiter between credentials and host.
    assert uri.count("@") == 1


def test_auth_source_is_configurable() -> None:
    assert _mongo(auth_source="wrc").uri.endswith("?authSource=wrc")
