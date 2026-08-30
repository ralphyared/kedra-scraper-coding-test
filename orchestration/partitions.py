"""The partition grid: every body crossed with every month.

The brief asks for scraping partitioned by body and by date range.
`MultiPartitionsDefinition` expresses exactly that as a first-class concept, so
the requirement becomes a framework feature rather than hand-rolled nested loops
whose failure and retry semantics have to be invented from scratch.

What that buys, concretely: a failed body-month is retried on its own rather
than restarting the range; each partition's record count reconciles
independently; and the UI shows a grid where a gap is visible instead of being
buried in a log.
"""

from __future__ import annotations

from dagster import (
    MonthlyPartitionsDefinition,
    MultiPartitionsDefinition,
    StaticPartitionsDefinition,
)

from kedra_scraper.bodies import BODIES
from kedra_scraper.config import get_settings

_settings = get_settings()

BODY_DIMENSION = "body"
MONTH_DIMENSION = "month"

# Static rather than dynamic: the four bodies are a fixed property of the
# source, not something discovered at runtime. A new tribunal would be a code
# change and deserves to be reviewed as one.
body_partitions = StaticPartitionsDefinition([body.slug for body in BODIES])

# Bounds come from configuration so the size of the grid is not compiled in.
# The default window is Q1 2024, which across four bodies is 12 partitions --
# the backfill used as the demo.
month_partitions = MonthlyPartitionsDefinition(
    start_date=_settings.partition_window_start,
    end_date=_settings.partition_window_end,
)

decision_partitions = MultiPartitionsDefinition(
    {BODY_DIMENSION: body_partitions, MONTH_DIMENSION: month_partitions}
)
