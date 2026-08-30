"""Scrapy settings, derived from the project's own configuration.

Nothing is hardcoded here: every tunable reads from `.env` through
`config.py`, so the politeness profile can be changed for a different
environment without editing code. Importing this module validates the
configuration, so a bad value fails before the first request rather than
mid-crawl.
"""

from __future__ import annotations

from kedra_scraper.config import get_settings

_settings = get_settings()

BOT_NAME = "kedra-scraper"
SPIDER_MODULES = ["kedra_scraper.scraper.spiders"]
NEWSPIDER_MODULE = "kedra_scraper.scraper.spiders"

# --------------------------------------------------------------------------
# Identity and permission
# --------------------------------------------------------------------------
USER_AGENT = _settings.scraper.user_agent

# Obeyed deliberately. Most of what we fetch is permitted because robots.txt
# disallows capitalised paths (`/en/Cases/`) while the real links are lowercase
# (`/en/cases/`), and robots matching is case-sensitive. The Equality Tribunal
# is the exception: its attachments really are under a disallowed prefix, so
# they are skipped and logged with a reason rather than fetched. See
# tests/test_robots.py, which pins all of this against the live file.
ROBOTSTXT_OBEY = _settings.scraper.robotstxt_obey

# --------------------------------------------------------------------------
# Politeness
#
# Requirement 1 asks for the fastest crawl that does not get blocked. AutoThrottle
# adapts the delay to the server's observed latency, so it slows down when the
# site is under load instead of hammering it at a fixed rate that happened to be
# safe when it was measured.
# --------------------------------------------------------------------------
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = _settings.scraper.autothrottle_target_concurrency
AUTOTHROTTLE_DEBUG = False

# The ceiling AutoThrottle is allowed to reach, not a target.
CONCURRENT_REQUESTS = _settings.scraper.concurrent_requests
CONCURRENT_REQUESTS_PER_DOMAIN = _settings.scraper.concurrent_requests
DOWNLOAD_DELAY = 0  # AutoThrottle owns the delay; a fixed one would fight it.
DOWNLOAD_TIMEOUT = _settings.scraper.download_timeout

# --------------------------------------------------------------------------
# Resilience
# --------------------------------------------------------------------------
RETRY_ENABLED = True
RETRY_TIMES = _settings.scraper.retry_times
# Stated explicitly rather than inherited. These are the codes worth retrying:
# transient server faults, request timeout, and rate limiting. 429 in particular
# must be retried with backoff rather than treated as a hard failure.
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 408, 522, 524]

# --------------------------------------------------------------------------
# Development cache
#
# Off by default. When on, responses are replayed from disk so that iterating on
# parsing does not re-hit the site -- which is both faster and politer. It must
# be off for a real run, or "freshly scraped" data could be hours old.
# --------------------------------------------------------------------------
HTTPCACHE_ENABLED = _settings.scraper.httpcache_enabled
HTTPCACHE_DIR = ".scrapy/httpcache"
HTTPCACHE_EXPIRATION_SECS = 0

# --------------------------------------------------------------------------
# Pipelines: blob first, then metadata. The order is load-bearing -- see the
# module docstring in pipelines.py.
# --------------------------------------------------------------------------
ITEM_PIPELINES = {
    "kedra_scraper.scraper.pipelines.ObjectStoragePipeline": 100,
    "kedra_scraper.scraper.pipelines.MongoMetadataPipeline": 200,
}

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
# Scrapy's own logging is left to propagate into the root logger, which
# logging_setup.py has already pointed at a JSON formatter. Setting LOG_FORMAT
# here would produce plain text and defeat that.
LOG_ENABLED = True
LOG_LEVEL = _settings.log_level

FEED_EXPORT_ENCODING = "utf-8"
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

# The site sends no gzip, so responses arrive uncompressed regardless; the
# middleware stays enabled so that if the site ever turns compression on, the
# crawl gets the benefit without a code change.
COMPRESSION_ENABLED = True
