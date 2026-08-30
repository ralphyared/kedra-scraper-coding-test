# WRC decision scraping pipeline

A Scrapy pipeline that scrapes decisions from Ireland's [Workplace Relations
Commission](https://www.workplacerelations.ie), partitioned by issuing body and
date range, into a landing zone (S3-compatible object storage + MongoDB) and
then into a cleaned curated zone, orchestrated by Dagster.

Design decisions and their reasoning are in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## What it does

```
                    ┌──────────── Dagster: body × month partitions ────────────┐
  workplace         │                                                          │
  relations.ie ─────┼──►  landing_documents  ──────►  curated_documents        │
                    │     (scrapy subprocess)         (clean / rename)          │
                    └──────────┬──────────────────────────┬────────────────────┘
                               ▼                          ▼
                    MinIO  wrc-landing/          MinIO  wrc-curated/
                    Mongo  decisions             Mongo  curated_decisions
```

The **landing zone** holds documents exactly as served and is never modified.
The **curated zone** is derived from it: page furniture stripped, documents
renamed to `identifier.ext`, a few fields lifted out of the prose. If the
cleaning rules turn out to be wrong, the curated zone is rebuilt from the
landing zone and nothing scraped is lost.

Four bodies are covered: the Workplace Relations Commission, the Labour Court,
the Employment Appeals Tribunal and the Equality Tribunal.

## Prerequisites

| Tool | Why |
|---|---|
| [Docker](https://docs.docker.com/get-docker/) + Compose | MongoDB and MinIO |
| [uv](https://docs.astral.sh/uv/) | dependency resolution and the virtualenv |
| `make` | optional — every target is a one-line shim you can run directly |

Python itself is not a prerequisite: `uv` fetches the pinned 3.12 interpreter.

## Quick start

```bash
cp .env.example .env      # every setting, with working local defaults
uv sync                   # create .venv from uv.lock
make up                   # Mongo + MinIO, buckets created, waits until healthy
make check                # ruff, mypy, 123 offline tests
```

Then crawl a month and transform it:

```bash
make crawl     START=2024-01-01 END=2024-01-31 BODY=wrc
make transform START=2024-01-01 END=2024-01-31
uv run kedra stats
```

## Worked example

Crawling the WRC for January 2024 — the site reports 234 decisions for that
range:

```bash
$ make crawl START=2024-01-01 END=2024-01-31 BODY=wrc
{"event": "partition_started", "body": "workplace_relations_commission", ...}
{"event": "listing_first_page", "total_reported": 234, "pages": 24}
{"event": "partition_finished", "total_reported": 234, "items_emitted": 234,
 "skipped_unchanged": 0, "duplicate_rows": 0, "failures": 0,
 "unaccounted": 0, "reconciled": true}
```

Run the identical command again and nothing is written:

```bash
{"event": "partition_finished", "total_reported": 234, "items_emitted": 0,
 "skipped_unchanged": 234, "unaccounted": 0, "reconciled": true}
```

`reconciled` is the single boolean worth watching. Every listing row the site
reported lands in exactly one bucket:

```
total_reported == items_emitted + skipped_unchanged + duplicate_rows + failures
```

Confirm the stored data is intact — every metadata record points at an object
that exists, and its hash still matches:

```bash
$ uv run kedra verify
checked=929 missing=0 hash_mismatch=0
```

## Orchestration

```bash
make dagster     # UI on http://localhost:3000
```

Assets are partitioned by `body × month`; the window is set by
`PARTITION_WINDOW_START` / `_END` and defaults to Q1 2024, giving a
12-partition grid. Backfill from the UI, or from the command line:

```bash
make backfill
```

Each partition carries an asset check, `every_row_is_accounted_for`, which
fails if any listing row went unexplained or if the crawl's own count does not
match what reached the database.

## Commands

Run `make help` for the full list.

| Target | |
|---|---|
| `make up` / `down` / `destroy` | start; stop keeping data; stop deleting data |
| `make crawl START= END= BODY=` | crawl a range, one subprocess per partition |
| `make transform START= END=` | derive the curated zone |
| `make dagster` / `backfill` | orchestration UI; materialise every partition |
| `make check` | ruff, mypy and the test suite, as CI would |

The CLI is also usable directly: `kedra crawl`, `kedra transform`,
`kedra stats`, `kedra verify`. The spider runs standalone too —
`uv run scrapy crawl wrc -a body=wrc -a start=2024-01-01 -a end=2024-01-31` —
because nothing in the library imports Dagster.

## Configuration

Every setting lives in `.env`, documented in
[`.env.example`](.env.example); nothing is hardcoded. The same file configures
both the containers and the application, so the port Mongo binds and the port
the app dials cannot drift apart.

| Group | Settings |
|---|---|
| Mongo | `MONGO_USERNAME`, `MONGO_PASSWORD`, `MONGO_HOST`, `MONGO_PORT`, `MONGO_DB`, `MONGO_AUTH_SOURCE` |
| Object storage | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`, `LANDING_BUCKET`, `CURATED_BUCKET` |
| Politeness | `SCRAPER_CONCURRENT_REQUESTS`, `SCRAPER_AUTOTHROTTLE_TARGET_CONCURRENCY`, `SCRAPER_RETRY_TIMES`, `SCRAPER_DOWNLOAD_TIMEOUT`, `SCRAPER_USER_AGENT`, `SCRAPER_ROBOTSTXT_OBEY` |
| Partitioning | `PARTITION_GRANULARITY`, `PARTITION_WINDOW_START`, `PARTITION_WINDOW_END` |
| Other | `SCRAPER_HTTPCACHE_ENABLED`, `LOG_LEVEL`, `LOG_DIR` |

Pointing `S3_ENDPOINT_URL` at real AWS is all that separates this from running
against S3 — the code is plain `boto3`.

## Logs

JSON lines to stdout and to `logs/run-<run_id>.jsonl`. Scrapy, boto3 and pymongo
log through the same formatter, so a run is one machine-readable artifact:

```bash
cat logs/run-*.jsonl | jq 'select(.event=="partition_finished")'
```

## Tests

```bash
make test
```

123 tests, none of which touch the network. They run against real captured
responses in `tests/fixtures/` rather than synthetic HTML — synthetic markup
would only prove the parsers handle markup we invented. The fixtures include two
fetches of the same unchanged page, which differ by exactly one line and are why
the pipeline keeps two different hashes per document.

## Troubleshooting

**`make up` fails, or the app cannot reach Mongo.** Something else may hold port
27017 — a locally installed MongoDB is the usual culprit. Because `localhost`
resolves to IPv4 first, the application then talks to *that* server and reports
`Authentication failed` with perfectly correct credentials. Change `MONGO_PORT`
in your `.env`; nothing else needs touching.

**Docker commands hang after installing Docker Desktop.** Its installer enables
Windows components that only take effect on restart. Until you reboot, the CLI
and its named pipes exist but the engine never answers — `docker version` works
while `docker info` hangs indefinitely.

**`make` is not found on Windows.** `winget install ezwinports.make`, or just
read the recipe and run the command — every target is a one-line shim.

**Image pulls stall.** They are large (~1.8 GB). If layer byte counts stop
advancing entirely rather than moving slowly, the connection is the problem
rather than Docker.

**Equality Tribunal records show `status: partial`.** That is correct, not a
bug. Their PDFs are genuinely disallowed by `robots.txt`; the HTML abstract is
stored and the skip is recorded with a reason. See ARCHITECTURE.md.
