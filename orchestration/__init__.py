"""Dagster orchestration.

Kept outside the `kedra_scraper` package on purpose. The pipeline is runnable
without an orchestrator at all -- `kedra crawl` and `scrapy crawl wrc` both work
standalone -- and that stays true only while nothing in the library imports
Dagster. The dependency points one way: orchestration knows about the scraper,
the scraper knows nothing about Dagster.
"""
