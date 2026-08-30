"""Partition tests, including the calendar edge cases that silently lose data."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from kedra_scraper.partitions import Partition, iter_partitions


def test_quarter_splits_into_whole_months() -> None:
    parts = iter_partitions(date(2024, 1, 1), date(2024, 3, 31))
    assert [p.label for p in parts] == ["2024-01", "2024-02", "2024-03"]
    assert parts[0].start == date(2024, 1, 1)
    assert parts[-1].end == date(2024, 3, 31)


def test_february_length_follows_the_leap_year() -> None:
    """A hardcoded 28 would drop every 29 February decision, once every 4 years.

    That is precisely the kind of bug that passes review, passes tests written
    in a non-leap year, and quietly loses a day of records in production.
    """
    leap = iter_partitions(date(2024, 2, 1), date(2024, 2, 29))
    assert leap[0].end == date(2024, 2, 29)
    assert leap[0].days == 29

    common = iter_partitions(date(2023, 2, 1), date(2023, 2, 28))
    assert common[0].end == date(2023, 2, 28)
    assert common[0].days == 28


def test_partitions_are_clamped_to_the_requested_range() -> None:
    """Boundaries become `from=` and `to=` on a live search request.

    An unclamped first partition would query all of January when the caller
    asked from the 15th, scraping two weeks of records nobody requested and
    filing them under a range that was never asked for.
    """
    parts = iter_partitions(date(2024, 1, 15), date(2024, 3, 10))
    assert (parts[0].start, parts[0].end) == (date(2024, 1, 15), date(2024, 1, 31))
    assert (parts[1].start, parts[1].end) == (date(2024, 2, 1), date(2024, 2, 29))
    assert (parts[2].start, parts[2].end) == (date(2024, 3, 1), date(2024, 3, 10))


def test_a_clamped_partition_keeps_its_calendar_label() -> None:
    """Identity is the month a record belongs to, not the slice requested.

    Two runs covering different parts of January must agree on where those
    records live, otherwise the same document lands under two partition keys.
    """
    parts = iter_partitions(date(2024, 1, 15), date(2024, 1, 20))
    assert parts[0].label == "2024-01"
    assert parts[0].days == 6


def test_partitions_are_contiguous_and_non_overlapping() -> None:
    """Gaps lose records silently; overlaps scrape them twice.

    Neither shows up as an error, which is why it is asserted rather than
    assumed.
    """
    parts = iter_partitions(date(2023, 11, 3), date(2024, 2, 7))
    assert len(parts) == 4
    for earlier, later in pairwise(parts):
        assert later.start == earlier.end + timedelta(days=1)


def test_a_single_day_range_yields_one_partition() -> None:
    parts = iter_partitions(date(2024, 5, 6), date(2024, 5, 6))
    assert len(parts) == 1
    assert parts[0].days == 1


def test_weekly_partitions_start_on_monday() -> None:
    parts = iter_partitions(date(2024, 1, 3), date(2024, 1, 20), granularity="week")
    # 3 Jan 2024 is a Wednesday; its week is clamped to start on the 3rd.
    assert parts[0].start == date(2024, 1, 3)
    assert parts[1].start.weekday() == 0


def test_weekly_labels_use_the_iso_week_year_not_the_calendar_year() -> None:
    """30 December 2024 is a Monday belonging to ISO week 2025-W01.

    Labelling it `2024-W01` would collide with the genuine first week of 2024,
    merging records from opposite ends of the year into one partition.
    """
    parts = iter_partitions(date(2024, 12, 30), date(2025, 1, 5), granularity="week")
    assert parts[0].label == "2025-W01"


def test_daily_partitions_are_one_day_each() -> None:
    parts = iter_partitions(date(2024, 1, 1), date(2024, 1, 4), granularity="day")
    assert [p.label for p in parts] == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    assert all(p.days == 1 for p in parts)


def test_a_reversed_range_is_rejected() -> None:
    """Silently returning an empty list would report a clean run of zero work."""
    with pytest.raises(ValueError, match="is after end"):
        iter_partitions(date(2024, 3, 1), date(2024, 1, 1))


def test_partitions_are_immutable() -> None:
    """A partition is passed to a subprocess and logged; it must not be edited."""
    part = Partition(date(2024, 1, 1), date(2024, 1, 31), "2024-01")
    with pytest.raises(AttributeError):
        part.label = "tampered"  # type: ignore[misc]
