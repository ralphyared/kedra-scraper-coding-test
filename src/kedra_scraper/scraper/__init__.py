"""The Scrapy project: fetching and persistence only.

All parsing lives in `kedra_scraper.parsers` as pure functions, so this package
is deliberately thin. The spider decides *what* to request and the pipelines
decide *where* bytes go; neither knows how to read the site's markup.
"""
