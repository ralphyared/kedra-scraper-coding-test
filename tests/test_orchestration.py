"""Tests for the orchestration layer's own logic.

Dagster's machinery is not under test here -- its scheduling and retries are the
framework's problem. What is tested is the translation between Dagster's
partition keys and the date range the spider is given, because an off-by-one
there loses real decisions with no error anywhere.
"""

from __future__ import annotations

from datetime import date

from orchestration.assets import month_bounds, partition_label
from orchestration.partitions import BODY_DIMENSION, MONTH_DIMENSION, decision_partitions


def test_month_bounds_covers_the_whole_month() -> None:
    """Dagster names a monthly partition by its first day; the spider needs both."""
    assert month_bounds("2024-01-01") == (date(2024, 1, 1), date(2024, 1, 31))


def test_february_length_follows_the_leap_year() -> None:
    """The bug a hardcoded 28 would cause is invisible.

    Every decision published on 29 February would be outside the queried range,
    the crawl would reconcile against a total that also excluded them, and
    nothing would report a problem.
    """
    assert month_bounds("2024-02-01")[1] == date(2024, 2, 29)
    assert month_bounds("2023-02-01")[1] == date(2023, 2, 28)


def test_thirty_day_months_are_not_given_thirty_one() -> None:
    """The opposite error: querying 31 April is a date that does not exist."""
    assert month_bounds("2024-04-01")[1] == date(2024, 4, 30)
    assert month_bounds("2024-06-01")[1] == date(2024, 6, 30)


def test_december_ends_on_the_thirty_first() -> None:
    assert month_bounds("2024-12-01") == (date(2024, 12, 1), date(2024, 12, 31))


def test_partition_label_matches_what_the_pipeline_stores() -> None:
    """The label written to Mongo and to object keys must agree with this.

    A mismatch would make the asset check count records under a partition name
    the crawl never used, and report zero stored for a partition that worked.
    """
    assert partition_label(date(2024, 1, 1)) == "2024-01"
    assert partition_label(date(2024, 12, 1)) == "2024-12"


def test_the_grid_is_every_body_crossed_with_every_month() -> None:
    """Four bodies over the configured Q1 window is the 12-partition demo."""
    dimensions = {d.name for d in decision_partitions.partitions_defs}
    assert dimensions == {BODY_DIMENSION, MONTH_DIMENSION}

    by_name = {d.name: d.partitions_def for d in decision_partitions.partitions_defs}
    bodies = by_name[BODY_DIMENSION].get_partition_keys()
    months = by_name[MONTH_DIMENSION].get_partition_keys()
    assert len(bodies) == 4
    assert len(months) == 3
    assert len(bodies) * len(months) == 12


def test_every_body_in_the_registry_is_a_partition() -> None:
    """The grid must not silently omit a body.

    Deriving the dimension from the registry rather than repeating the list is
    what guarantees this, so the test pins that they stay in step.
    """
    from kedra_scraper.bodies import BODIES

    by_name = {d.name: d.partitions_def for d in decision_partitions.partitions_defs}
    assert set(by_name[BODY_DIMENSION].get_partition_keys()) == {b.slug for b in BODIES}
