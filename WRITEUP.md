# Write-up

> Draft. Q1 and Q2 are for the owner to write. Q3, Q4 and the last section were drafted by Claude
> from the build's measurements and records, for the owner to review and rewrite in their own words.

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

- **All of the application code, tests, fixtures and docs in this repository were written by
  Claude Code (Claude Opus 5.5)**, in the commits from `7aea802` onwards, each of which carries a
  `Co-Authored-By: Claude` trailer. That includes the parts the plan originally reserved for the
  owner to hand-write (the variable registry, the argparse signature, the table DDL and the
  missing-key SQL): the owner delegated those, and they are in their own commits.
- The first commit, `2960d6c` "Plan and tooling", predates the build session and has no trailer.
  It holds the project's tooling: `CLAUDE.md`, the rule-engine policy, the prompt-logging hook and
  the vendored skills. **[OWNER]** Say who wrote which of these, and how.
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

## Failure modes and known limitations (drafted by Claude)

A review in turn 6 went through each external dependency (network, disk, database). For each, it
checked a sustained outage, a local resource error and one item failing mid-run. It also checked
whether failures are remembered or retried, whether any error handler can itself raise, and what
completed work a failure loses.

What holds:
- A failed variable loses only its own (hour, variable) block.
- A missing or refused `.idx` loses only its hour, and is fetched once.
- A bad cached copy is reported once and downloaded again.
- Rows are committed one hour at a time, so a failure never loses a stored hour.
- Every failure exits 1, and a re-run fetches only what is missing.

Fixed in turn 6 (medium):
- **The CLI's error handler could itself crash.** It printed the first line of the error message,
  which raised `IndexError` when the message was empty (`OSError()`, for example). It now prints the
  exception's type instead.
- **A sustained outage dragged on hour by hour.** Each later hour spent a full retry cycle on its
  `.idx`: 3 attempts and 15 s of backoff per hour. That is about 12 minutes for a 48-hour run when
  connections fail fast, and up to about 2.6 hours when each attempt hits the 60 s timeout. A
  request that fails all three attempts now stops the run. The rows the current hour had already
  gathered are stored first.
- **A failed insert did not stop the run.** After a full disk or an I/O error, every later hour
  still downloaded its messages and then failed to insert. With the cache on the same full disk, a
  default run would download about 0.8 GB and keep none of it. A failed insert now stops the run.

Known limitations (low, not fixed):
- **Cache write failures are not remembered.** With an unwritable cache, each block prints its own
  "could not cache" line: up to 528 for a default run. Each failure costs only a local system call.
- **A failed cache write can leave a `.part` file.** It is ignored, and overwritten by the next
  write of that message, but never deleted.
- **A server that ignores the `Range` header** and answers 200 makes the tool read the whole file
  (about 150 MB) into memory before the 206 check rejects it. S3 honours `Range`; a proxy might not.
- **The 60 s timeout applies to each socket read, not to the whole request.** A connection that
  sends a byte every 59 s never times out.
- **The default date can take about 2,000 requests to give up.** If listings succeed but come back
  empty (for example, NOAA moves the data), default-date discovery lists every date back to
  2020-12-03 before exiting 1.
- **A listing that repeats its continuation token would be paged forever.** S3 does not do this.
- **The summary line can be lost after a database failure.** If the database is unusable after a
  failed insert, the final missing-key query fails too. The CLI then prints the one-line database
  error instead of the summary line. Stored hours are kept.
- **Ctrl-C prints a Python traceback.** Stored hours are kept. The current hour's rows are lost, but
  its messages are already cached.
- **A closed or broken stderr makes the tool crash.** Printing a warning raises, and the CLI's own
  error line fails the same way, so the tool ends with a traceback.
- **A corrupted cache file is not always caught.** GRIB2 has no checksum, so a cached file damaged
  inside its data section but with intact framing, identity and grid hash would be decoded as-is.
  This is the cache checksum manifest listed under Q3.

## How the rule engine shaped this build (drafted by Claude)

The engine's latest report is committed as [docs/ruler-report.txt](docs/ruler-report.txt). It
passed the repository's leak scan, which was the condition for committing it. The same report is
also kept with the private working notes.

`llm-rule-engine` sits in Claude Code's hooks. It blocks every mutating action (file edit, shell
command that writes, script run) until Claude files a **preflight**: a verdict and a specific note
on each of its rules (understood patterns, architecture, simpler alternatives, less code, package
security, vulnerable dependencies, slowdown, real tests plus mutation testing, blast radius, no
shortcuts). It also refuses to let the turn end until a **postflight** cites machine-recorded
evidence: a passing test run after the last edit, and a mutation run on every changed source file
with zero surviving mutants. A surviving mutant can only be waived by the owner, case by case.

**Gated turns.** The session had six turns. Only those that changed something need a preflight
and a postflight.

| Turn | What it did | Gate |
| --- | --- | --- |
| 1 | The whole build, through review | Preflight accepted first time; postflight rejected once, then accepted (below) |
| 2 | A question: had the work been reviewed? | Read-only, no gate needed |
| 3 | The three reviewers re-read the last docs commits; two doc errors fixed | Preflight and postflight accepted first time |
| 4 | `ruler doctor` (only the Go tools govulncheck and go-mutesting missing; this repo has no Go) | Read-only, no gate needed |
| 5 | Fixes from an outside review (below) | Preflight accepted first time; postflight rejected once (the tests were already committed, so R9 needed a note naming which tests cover the changed files), then accepted; `ruler report` exit 0, PASS ([docs/ruler-report.txt](docs/ruler-report.txt)) |
| 6 | An empty-message crash in the CLI's error handler, then the failure-mode review above (three medium findings fixed) | Preflight accepted first time. The stop hook blocked once: Claude ended the turn while a mutation run was still going in the background. The postflight was rejected once, for two reasons. First, the engine records each mutation run as a code change, so the earlier test run counted as stale and was re-run. Second, the edits were made by scripts the engine cannot attribute to files, so R9 needed a waiver; it names the three mutation runs on the changed files (0 survivors). The postflight was then accepted; `ruler report` exit 0, PASS |

Turn 5 fixed four issues, test-first:
- A failed `.idx` fetch now ends its hour once, instead of being retried for every variable (three
  attempts each) during an outage.
- A locked or unusable `data.db`, or any DuckDB or OS error during a run, is now a one-line error
  with exit 1, not a traceback.
- `read_cached` and `write_atomic` are now mutation-tested; only the raw HTTP call is excluded.
- The variable registry is now a read-only mapping.

**Every time it blocked something** (all before the preflight, none twice):

1. Writing the plan, converted to text, into a scratch file: denied ("gate closed: output
   redirection"). Claude read the HTML directly instead.
2. Running a read-only `python3 -c` to convert the same file: denied ("interpreter execution").
3. A compound read-only command that also printed `.claude/settings.json`: denied as "touches
   enforcement/config infrastructure, never allowed". The settings were not needed, so the command
   was split and that part dropped.

**Every time it changed what was done:**

- **Dependency audit.** Editing `pyproject.toml` deferred the audit until a lockfile existed.
  `uv.lock` was then scanned with osv-scanner: 0 findings, and again after each later edit.
- **Exit-masked tests.** Test runs piped into `grep` or `tail` (four in turn 1, two red runs in
  turn 6) were flagged as not citable, because the pipe hides pytest's exit code. Each was re-run on
  its own.
- **Mutation testing**, required with zero survivors, drove most of the test design:
  - It exposed two bugs in the mutation tooling itself. mutmut 2 counts only exit code 1 as
    "killed", so mutants that crash at import (exit 4) were reported as survivors. Its timeout
    also could not stop pytest started through `uv run` or `sh`, which left one run hung for
    17 minutes. The runner in `pyproject.toml` now calls pytest directly and maps any failure
    to exit 1.
  - Real test gaps it found, now covered by tests:
    - a registry entry could be changed at run time (each `Variable` is now a frozen dataclass;
      since turn 5 the `VARIABLES` mapping itself is read-only too);
    - a message assertion matched a substring, so a corrupted message still passed;
    - the 3 km distance warning was not tested at its boundary;
    - a cache-scan `continue` was not tested where `break` would differ.
  - Lines where no test could tell the mutant apart, rewritten so the question disappears:
    - retry backoff `2**attempt` (equal to `2*attempt` for attempts 1 and 2);
    - an unused `problem = ""` initialiser;
    - a `""` fallback;
    - `values[mask] = np.nan`, where `None` also writes NaN;
    - an `axis=-1` literal;
    - a modulo-based longitude conversion;
    - a `found = None` placeholder.

  Final result: 0 surviving mutants in all six modules. The raw HTTP call (`source._http_get`) is
  the only code excluded from mutation, marked `# pragma: no mutate`. One infinite-loop mutant
  (walking the default date forwards) is caught by mutmut's timeout.
- **Survivors approved by the owner:** none. None had to be brought to the owner, because every
  survivor was either killed by a better test or removed by rewriting the line.

The engine also records each mutmut run as evidence when it starts. That includes one `source.py`
run that Claude stopped once it hung (evidence `ev-a78724`), which is therefore not cited.

**The postflight (turn 1).**

- **First submission: rejected, with 9 errors.**
  - One note did not name a changed file.
  - The slowdown rule's 1 ms latency budget does not fit a network-bound batch job, so the
    over-budget estimate needed an explicit justification.
  - All the test and mutation evidence was stale. The engine counts every shell command it cannot
    classify as a possible code change. That included the wait loops between mutation runs and the
    reviewer agents' scratch scripts, so each one made the earlier evidence look out of date.
- **What was done about it.** All edits were finished and committed. Then the full test suite and
  the six mutation runs were repeated back to back, with only read-only commands in between, and
  the postflight was resubmitted.
- **The resubmission was accepted**, and `ruler report` then exited 0 with `VERDICT: PASS`
  ([docs/ruler-report.txt](docs/ruler-report.txt)). Its "postflights with FAIL: 0" does not hide
  the first rejection. As Claude reads it, the report counts accepted postflights that carry a FAIL
  verdict. A rejected submission is never accepted, so it isn't in that count.

