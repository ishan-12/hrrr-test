# Write-up

## Q1. Time spent and difficulties

> **[OWNER]** To be written by the owner from `docs/worklog.md`. The notes below are facts from the
> build session that may help; replace them with your own account.

Difficulties met during the build (from the session record and git history):

- **Variable identity.** `grib_ls` prints `typeOfFirstFixedSurface` as `sfc` for every field, both
  the ground surface (code 1) and heights above ground (code 103); the code must compare numbers.
  Surface roughness is in discipline 2, so category/number alone could match the wrong field.
- **DuckDB 2.0 and `ON CONFLICT`.** With `INSERT ... SELECT unnest(?) ... ON CONFLICT DO NOTHING`,
  DuckDB raised a primary-key violation on a re-insert unless the SELECT cast each column to the
  table's types. A store test caught it before any end-to-end code existed.
- **ecCodes and garbage input.** `codes_new_from_message` accepts some invalid bytes and only fails
  on the first key lookup, so every ecCodes error is turned into a "bad message" error.
- **Mutation testing with mutmut 2.** It counts only exit code 1 as a killed mutant; a mutant that
  breaks conftest import makes pytest exit 4 and was reported as a survivor. Its timeout also could
  not stop pytest when started through `uv run`. The runner in `pyproject.toml` now calls pytest
  directly and maps any failure to exit 1.

## Q2. AI assistants and how they were used

> **[OWNER]** To be written by the owner, with real prompts from `docs/ai-log.md` and the work log.
> Facts for this section:

- **All code in this repository was written by Claude Code (Claude Opus 5.5)**, as every commit's
  `Co-Authored-By` trailer shows. That includes the parts the plan originally reserved for the
  owner to hand-write (the variable registry, the argparse signature, the table DDL and the
  missing-key SQL): the owner delegated those, and they are in their own commits.
- The plan (private working notes, not part of the submission) came from two AI reviews: one by three reviewer
  agents, one by GPT-6 Astra with Claude Opus. The owner settled the five open decisions (D1-D5).
  ADR 0001 records them; its text was drafted by Claude and awaits the owner's wording.
- The facts about HRRR were not taken from the plan. Claude re-derived them from `.idx` files,
  byte-ranged messages and `grib_ls`/`grib_get`, then checked that they matched the plan's table
  ([docs/research/hrrr-variables.md](docs/research/hrrr-variables.md)).
- A rule engine (`llm-rule-engine`) gated every change Claude made: see the last section.

## Q3. Running it as a production service

**Scheduling.** A 06z run appears in the bucket from about 06:55 UTC and is complete around
08:05 UTC (49 of 49 files; a listing at 07:44 found 46). Two options, simplest first:

1. A scheduled job (cron, EventBridge Scheduler, Airflow) at 08:30 UTC that runs
   `hrrr-ingest points.txt --run-date <today>`, then again hourly until it exits 0. Because the tool
   is idempotent, re-running is the retry policy: each attempt fetches only what is still missing.
2. Event-driven: NOAA's open-data buckets publish new-object notifications (SNS). A small function
   counts the run's `.grib2.idx` arrivals and starts ingestion per forecast hour as each one lands,
   so f00 is queryable about an hour before the run completes. (The topic ARN needs confirming.)

The job would run in `us-east-1`, next to the bucket: transfer is faster and free.

**Storage for analysts.** A single DuckDB file has one writer and lives on one disk. For shared
access I would write each run's rows as Parquet to object storage, partitioned by
`run_date=YYYY-MM-DD/`, sorted by variable and location. Analysts then query it with DuckDB,
Athena/Trino or Spark directly, and adding a run never rewrites old files. Keep the same long-format
schema and primary key. A wide view (one column per variable) is easy to build on top. If analysts
need low-latency point lookups, load the same Parquet into ClickHouse or Postgres/TimescaleDB.

**Monitoring.**
- Completeness: every run should have points × variables × 48 rows. Alert when a run date is missing
  or short after 10:00 UTC, using the same missing-key query the tool already runs.
- Freshness: the age of the newest `run_time_utc`.
- The tool's own signals: exit code, blocks still missing, requests and bytes per run (already in its
  summary line). Ship them as metrics; alert on exit 1 twice in a row.
- Data sanity: per-variable ranges (e.g. 150-350 K for temperature, 0-100 % RH, solar flux >= 0 and
  0 at night), and a day-over-day jump check per location.
- Rejected messages: identity or grid check failures should never happen; any one is a page,
  because it means NOAA changed the product.

**Hardening not built (on purpose, for this scope):** pinning each object's ETag between the `.idx`
and the message reads, a checksum manifest for the cache, and a provenance table. No evidence was
found that NOAA rewrites published files, so these belong to the production version.

## Q4. Backfilling years of data

Measured for one run (9 points, 11 variables, 48 hours): **576 requests, 789 MB, 6 minutes,
sequential, on a laptop.** That is network-bound: CPU time was under 14 s.

At that rate one year of 06z runs is about 210,000 requests, 288 GB and 36 hours. What I would
change, roughly in order of payoff:

1. **Parallel downloads.** S3 serves many concurrent range requests. 32-64 workers per process,
   running in `us-east-1`, should take a run from 6 minutes to well under one. The work list is
   already explicit: (hour, variable) blocks from the missing-key query.
2. **Merge adjacent byte ranges.** Several of the 11 messages are neighbours in each file (UGRD and
   VGRD at 80 m and PRES are messages 60-62; DPT and RH are 74-75; the 10 m winds 77-78; VBDSF and
   VDDSF 128-129 at f00). Merging cuts about 11 requests per hour to about 6.
3. **One task per run date, many workers.** Each run date is independent, and the tool is already
   idempotent per run. Fan dates out to a queue (AWS Batch, Step Functions, Airflow), each task
   writing its own Parquet partition, so no workers share a DuckDB file.
4. **Don't keep raw messages.** For a backfill the cache is 0.75 GB per run with nothing to reuse
   it. Decode in memory and keep only the extracted values, or cache to cheap storage with a
   lifetime rule.
5. **Nearest-point lookup for many points.** It is brute force today: about 4 ms per point
   (measured) against 1.9 million grid points. That is fine for hundreds of points, but 100,000
   points would take about 7 minutes per grid. Build a KD-tree on the grid's unit vectors once (`scipy.spatial.cKDTree`).
   The grid hash (`md5Section3`) is the cache key, so the tree is reused across every run with the
   same grid.
6. **Decode cost.** ecCodes decodes whole 1.9-million-point fields (about 10 ms each, measured). Across processes
   that is cheap compared with the downloads, so I would only move it to a process pool.

Older data needs handling, not just speed. Before 2020-12-03 the 06z run stops at f36 or earlier.
Older files lack some fields (2016 files have no RH, SFCR, VBDSF or VDDSF). The HRRR version
changed several times. The tool already reports unpublished hours and missing variables and exits
1 without failing the rest. A backfill would record those gaps as expected, per period, instead of
retrying them forever.

## How the rule engine shaped this build

> Filled in from the engine's ledger at the end of the session. The engine's own report is kept with
> the private working notes, and a copy is committed only if it passes the repository's leak scan.

<!-- completed below at the end of the session -->
