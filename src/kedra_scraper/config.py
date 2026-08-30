"""Single source of configuration for the whole pipeline.

The brief's Configuration requirement says no hardcoded values, so every knob
lives in `.env` (documented in `.env.example`) and is read exactly once, here.

Values are *validated* on load rather than read ad hoc with `os.getenv`. A
missing or malformed setting therefore fails at startup with a precise error,
instead of surfacing hours into a crawl as an unexplained `None`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# `extra="ignore"` matters: `.env` is shared with docker-compose.yml and so
# contains container-only variables (MONGO_ROOT_PASSWORD, MINIO_ROOT_USER, ...)
# that no settings class declares. Without this they would be validation errors.
_BASE = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class MongoSettings(BaseSettings):
    """Metadata store connection.

    The connection string is assembled from its parts rather than stored whole.
    A literal MONGO_URI would duplicate the host and port that MONGO_HOST and
    MONGO_PORT already define -- and docker-compose.yml binds the container using
    MONGO_PORT, so the two would silently drift the moment anyone changed one and
    not the other.
    """

    model_config = SettingsConfigDict(**_BASE, env_prefix="MONGO_")

    host: str = "localhost"
    port: int = 27017
    username: str
    password: str
    # The container's entrypoint creates the root user in `admin`, not in the
    # application database, so credentials must be checked against admin.
    auth_source: str = "admin"
    db: str = "wrc"

    @property
    def uri(self) -> str:
        """MongoDB connection string.

        quote_plus on the credentials is not decoration: a password containing
        `@`, `:`, `/` or `?` would otherwise terminate the userinfo section early
        and produce a URI that either fails to parse or, worse, silently points
        at the wrong host.
        """
        return (
            f"mongodb://{quote_plus(self.username)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/?authSource={self.auth_source}"
        )


class S3Settings(BaseSettings):
    """Object store connection, S3-compatible.

    `endpoint_url` is what makes MinIO and real AWS interchangeable: boto3 talks
    to whatever is here, so moving to production is a config change, not a code
    change. Leaving it unset selects real AWS.
    """

    model_config = SettingsConfigDict(**_BASE, env_prefix="S3_")

    endpoint_url: str | None = None
    access_key: str
    secret_key: str
    region: str = "us-east-1"


class ScraperSettings(BaseSettings):
    """Politeness and resilience knobs for the Scrapy spider.

    Requirement 1 asks for the fastest crawl that does not get blocked. These
    are the ceiling AutoThrottle is allowed to reach, not a fixed rate.
    """

    model_config = SettingsConfigDict(**_BASE, env_prefix="SCRAPER_")

    concurrent_requests: int = Field(default=8, ge=1)
    autothrottle_target_concurrency: float = Field(default=4.0, gt=0)
    download_timeout: int = Field(default=60, ge=1)
    retry_times: int = Field(default=3, ge=0)
    robotstxt_obey: bool = True
    user_agent: str
    httpcache_enabled: bool = False


class Settings(BaseSettings):
    """Root settings object. Compose the sub-sections above."""

    model_config = _BASE

    landing_bucket: str
    curated_bucket: str

    # Monthly by default. See ARCHITECTURE.md: the densest body-month is roughly
    # 300 records, which is small enough to retry cheaply and large enough that
    # per-request overhead does not dominate.
    partition_granularity: Literal["month", "week", "day"] = "month"

    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    # default_factory, not a plain default: each sub-class reads the environment
    # when it is constructed, and a shared mutable default would be built once at
    # import time and never reflect a later environment change (notably in tests).
    #
    # The ignores below are all the same point. These classes declare required
    # fields with no defaults, so mypy types their zero-argument constructor as
    # returning Never and rejects the call. It cannot see that pydantic-settings
    # populates those fields from the environment at runtime. The ignore is
    # narrowed to the exact error code and the exact lines, so a genuinely wrong
    # constructor call anywhere else in this module is still reported.
    mongo: MongoSettings = Field(default_factory=MongoSettings)  # type: ignore[arg-type]
    s3: S3Settings = Field(default_factory=S3Settings)  # type: ignore[arg-type]
    scraper: ScraperSettings = Field(default_factory=ScraperSettings)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    A cached function rather than a module-level instance: importing this module
    then has no side effects, and a test can call `get_settings.cache_clear()` to
    re-read a patched environment. A module-level singleton would be fixed at
    first import and untestable.
    """
    return Settings()  # type: ignore[call-arg]  # see note on the fields above
