# Architecture

Scrapy spider → landing zone (MinIO + MongoDB) → transformation → curated zone,
orchestrated by Dagster over a `body × month` grid. Bytes live in object
storage, queryable facts in Mongo, each record pointing at its object. Blobs are
written **before** metadata, so a record can never reference a missing object.

## Partition size: one body-month

The unit of work, retry and reconciliation. The densest observed body-month is
the WRC at ~300 records — about 30 listing pages, under a minute — small enough
that failure is cheap to repeat, large enough that per-request overhead does not
dominate. Q1-2024 across four bodies is 895 records.

Partitioning by body is forced, not chosen: passing two `body` values does not
union them, it silently disables the filter and returns everything.

`PARTITION_GRANULARITY` switches to weekly or daily for dense backfills. At much
larger scale the split becomes adaptive — subdivide when a partition's reported
total exceeds a page threshold — since the site states the total before we fetch
any of it.

## Retries and rate limiting

AutoThrottle adapts delay to observed latency rather than enforcing a fixed
sleep, so the crawler slows when the site is loaded instead of hammering it at a
rate that merely happened to be safe when measured. Concurrency ceiling 8,
AutoThrottle targeting 4. Retries cover 429, 408 and 5xx with backoff.

Measured on the WRC January partition: **259 requests, all HTTP 200, zero
retries, 44.5 s — about 5.8 req/s.** No throttling was encountered, so this is a
**floor, not a discovered threshold**; deliberately probing a government site for
its 429 boundary was not a reasonable trade.

`robots.txt` is obeyed. Case pages are permitted because the disallow rules name
capitalised paths (`/en/Cases/`) while real links are lowercase, and robots
matching is case-sensitive. The **Equality Tribunal is the exception**: its PDFs
keep the capitalised `/en/Equality_Tribunal_Import/` prefix and are genuinely
disallowed. Those records store the HTML abstract, skip the PDF with reason
`robots_disallowed`, and are marked `status: partial` — the shortfall is
explained, not hidden. `tests/test_robots.py` proves this against the live file.

## Deduplication and change detection

**Deterministic identity.** `_id` is `<body_slug>:<identifier_slug>`, so
re-scraping upserts the same document and duplicates are impossible by
construction. Scoping by body matters: EAT identifiers are bare integers that
would otherwise collide.

**Two hashes, because one is insufficient.** Every case page embeds a
per-request `<!-- Elapsed time: N -->` comment, so two fetches of an unchanged
page differ. `file_hash` covers exactly the stored bytes; `content_hash` covers a
normalised form and is what change detection compares. Without the split every
document would look changed on every run, while appearing to detect changes
correctly.

**Conditional GET, where it works.** Case pages send `Cache-Control: no-cache`
with no validators, so HTML is always re-fetched. PDFs advertise both `ETag` and
`Last-Modified`, but only `If-None-Match` is honoured — `If-Modified-Since` is
ignored and the body resent. Building on the wrong header would look like working
conditional GET while saving nothing.

The landing zone is append-only: changed content goes to a version-suffixed key
rather than over its predecessor, compared against a content hash held as S3
object metadata so the check costs a HEAD, not a download.

A re-run of an unchanged partition writes **0 objects and 0 duplicates**.

## At 50+ sources

The shape holds; the hardcoded parts do not. A source registry replaces
`bodies.py` and the spider becomes config-driven with per-source parser adapters
behind one interface — parsing is already pure functions over HTML text, so this
is dispatch, not rewriting. Politeness profiles become per-source, since one slow
site should not throttle 49 others.

Partition state moves to a metastore instead of being re-derived from each site
per run, and execution moves behind a queue so sources distribute across workers.
Reconciliation generalises: each source declares how to obtain its expected
count, and the per-partition `reconciled` boolean becomes a per-source SLA. The
two-zone split and the blob-before-metadata ordering scale unchanged.
