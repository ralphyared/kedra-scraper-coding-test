"""Splitting a date range into the units the crawl is actually executed in.

The brief asks for scraping partitioned by body and date range. A partition is
the unit of work, of retry, and of reporting: if one fails, only it is re-run,
and its record count is reconciled independently of every other.

Monthly is the default. The densest body-month observed is the WRC at roughly
300 records -- about 30 listing pages, a few minutes of crawling. That is small
enough that a failure costs little to repeat, and large enough that per-request
overhead does not dominate. Weekly and daily exist for backfilling eras dense
enough that a month would be unwieldy, which is the shape the same code would
take at the brief's hypothetical 1000x scale.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Granularity = Literal["month", "week", "day"]


@dataclass(frozen=True, slots=True)
class Partition:
    """One unit of crawl work: an inclusive date range plus its stable label.

    `label` is what appears in object keys, in Mongo's `partition_date`, and as
    the Dagster partition key, so it must be stable and sortable as text.
    """

    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        """Inclusive length in days. Useful for logging and for sizing checks."""
        return (self.end - self.start).days + 1


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _month_firsts(start: date, end: date) -> Iterator[date]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _week_mondays(start: date, end: date) -> Iterator[date]:
    # `weekday()` is 0 for Monday, so this snaps back to the ISO week start.
    cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=7)


def iter_partitions(start: date, end: date, granularity: Granularity = "month") -> list[Partition]:
    """Split the inclusive range `start`..`end` into partitions.

    Partitions are *clamped* to the requested range: asking for 15 Jan to 10 Mar
    yields 15-31 Jan, all of Feb, and 1-10 Mar -- not three whole months. The
    boundaries become `from=` and `to=` on a live search request, so an unclamped
    first partition would scrape two weeks of January that were never asked for
    and file them under a range the caller did not request.

    Labels still name the whole calendar unit (`2024-01`), because a partition's
    identity is the month it belongs to, not the sub-range that happened to be
    requested. Two runs covering different slices of January agree on where the
    records belong.
    """
    if start > end:
        raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")

    partitions: list[Partition] = []

    if granularity == "month":
        for first in _month_firsts(start, end):
            last = _last_day_of_month(first.year, first.month)
            partitions.append(
                Partition(max(first, start), min(last, end), f"{first.year:04d}-{first.month:02d}")
            )
    elif granularity == "week":
        for monday in _week_mondays(start, end):
            sunday = monday + timedelta(days=6)
            iso_year, iso_week, _ = monday.isocalendar()
            # ISO week-numbering year, not the calendar year: 30 Dec 2024 is a
            # Monday belonging to 2025-W01, and labelling it 2024-W01 would
            # collide with the actual first week of 2024.
            partitions.append(
                Partition(max(monday, start), min(sunday, end), f"{iso_year:04d}-W{iso_week:02d}")
            )
    else:
        cursor = start
        while cursor <= end:
            partitions.append(Partition(cursor, cursor, cursor.isoformat()))
            cursor += timedelta(days=1)

    return partitions
