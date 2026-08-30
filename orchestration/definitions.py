"""Dagster entry point.

    dagster dev -m orchestration.definitions

Deliberately thin. Everything it wires together is usable without Dagster --
the spider runs from `scrapy crawl`, a range runs from `kedra crawl` -- so the
orchestrator adds partition tracking, retries and a UI rather than being the
only way to run anything.
"""

from __future__ import annotations

from dagster import Definitions

from .assets import every_row_is_accounted_for, landing_documents

defs = Definitions(
    assets=[landing_documents],
    asset_checks=[every_row_is_accounted_for],
)
