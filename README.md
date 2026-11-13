# hrrr-ingest

A command-line tool that downloads NOAA HRRR forecasts (the 06z run, up to 48 hours) for a list of
points and stores them in a local DuckDB file, `data.db`, in the `hrrr_forecasts` table.

```bash
hrrr-ingest points.txt --run-date 2025-04-24 --variables temperature_2m,surface_pressure --num-hours 2
```

It downloads only the GRIB2 messages it needs (HTTP byte ranges located through each file's `.idx`
index), keeps them in a local cache, and never stores a row twice. Running the same command again
downloads nothing and adds nothing.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer; uv installs Python if needed.
ecCodes, the GRIB2 decoder, comes as a binary wheel (`eccodeslib`), so no system packages are
required on macOS or Linux.

```bash
git clone <this repo> && cd hrrr-ingest
uv sync                      # creates .venv with the pinned dependencies
uv run hrrr-ingest --help
uv run pytest                # offline test suite, a few seconds
```

Or with Docker (see [Docker](#docker)).

## Running it

```bash
uv run hrrr-ingest points.txt [--run-date YYYY-MM-DD] [--variables a,b,...] [--num-hours N]
```

| Argument | Meaning | Default |
| --- | --- | --- |
| `points.txt` | Text file, one `latitude,longitude` per line (decimal degrees, longitude -180..180). Blank lines are ignored; duplicates collapse. | required |
| `--run-date` | Date of the 06z run, UTC. | The newest UTC date whose 06z run is fully published (all of f00-f48). |
| `--variables` | Comma-separated names from the table below. | All 11 |
| `--num-hours` | Forecast hours f00 to f(N-1), 1 <= N <= 48. `--num-hours 2` stores 06:00 and 07:00 UTC. | 48 |

`data.db` and the cache folder `.cache/hrrr/` are created in the current directory.
[`points.txt`](points.txt) holds the spec's 9 example points.

The spec mentions "each of the dates in the date range"; the CLI it specifies takes a single
`--run-date`, so that is what is implemented.

### Variables and units

Values are stored exactly as published, in native GRIB2 units.

| Stored name | HRRR `.idx` label | Units |
| --- | --- | --- |
| `surface_pressure` | `PRES:surface` | Pa |
| `surface_roughness` | `SFCR:surface` | m |
| `visible_beam_downward_solar_flux` | `VBDSF:surface` | W/m² |
| `visible_diffuse_downward_solar_flux` | `VDDSF:surface` | W/m² |
| `temperature_2m` | `TMP:2 m above ground` | K |
| `dewpoint_2m` | `DPT:2 m above ground` | K |
| `relative_humidity_2m` | `RH:2 m above ground` | % |
| `u_component_wind_10m` | `UGRD:10 m above ground` | m/s |
| `v_component_wind_10m` | `VGRD:10 m above ground` | m/s |
| `u_component_wind_80m` | `UGRD:80 m above ground` | m/s |
| `v_component_wind_80m` | `VGRD:80 m above ground` | m/s |

Each downloaded message is also checked by what it says it is (GRIB2 discipline, category, number,
level type and value, an instantaneous product (template 0), forecast hour, run date and time, and
grid), not only by its `.idx` label.
How each identity was verified: [docs/research/hrrr-variables.md](docs/research/hrrr-variables.md).

Conventions:

- **Winds are grid-relative.** HRRR's u/v follow the Lambert grid's x/y axes, not true east/north
  (the rotation is sin(38.5°) × (longitude + 97.5°): about 6° over Alabama, about 17° on the
  west coast and 19° in Maine). They are stored as
  published, which is also what ecCodes, cfgrib and Herbie return. Rotate them downstream if you
  need earth-relative winds.
- **Times are naive UTC.** `run_time_utc` is the run date at 06:00; `valid_time_utc` is run time
  plus the forecast hour.
- **Coordinates are the grid point's**, not the input point's: the closest HRRR grid point by
  great-circle distance, longitude in -180..180, stored as FLOAT. Two input points that share a grid
  cell produce one set of rows. Points are never rejected for distance; one more than 3 km from its
  grid point gets a warning.
- **Night-time solar flux is a real zero.** Missing values are never stored, and never replaced by
  zero.
- **The visible beam flux behaves like direct-normal irradiance**, not beam on a horizontal surface:
  at one checked point VBDSF × sin(sun elevation) + VDDSF matched HRRR's total downward shortwave
  flux (DSWRF). So VBDSF + VDDSF is not global horizontal irradiance.

## Checking the result

With the [DuckDB CLI](https://duckdb.org/docs/installation/) (`duckdb -readonly data.db`), or
without installing anything else, from the project folder:

```bash
uv run python -c "import duckdb; print(duckdb.connect('data.db', read_only=True).sql('DESCRIBE hrrr_forecasts'))"
```

Put any of the queries below in place of `DESCRIBE hrrr_forecasts` the same way.

```sql
-- 1. Schema: the spec's 7 columns and types, in order
DESCRIBE hrrr_forecasts;

-- 2. No duplicate keys (expect no rows)
SELECT run_time_utc, valid_time_utc, variable, latitude, longitude, count(*)
FROM hrrr_forecasts GROUP BY ALL HAVING count(*) > 1;

-- 3. Rows per run and variable (spec example: 9 points x 2 hours = 18 per variable)
SELECT run_time_utc, variable, count(*) AS n_rows, min(valid_time_utc), max(valid_time_utc)
FROM hrrr_forecasts GROUP BY ALL ORDER BY ALL;
```

The table also has a primary key on (run_time_utc, valid_time_utc, variable, latitude, longitude),
so a duplicate cannot be inserted at all.

## Re-runs, the cache, and failures

- Before downloading, the tool asks DuckDB which (hour, variable) blocks have any requested row
  missing, and works only on those. A re-run of a finished command with an explicit `--run-date`
  and an intact cache makes no network requests (the default date costs one or two listing
  requests; a deleted cache costs one message download to read the grid). Adding a point usually
  needs no download either: the messages are already cached.
- Each downloaded message is saved as `.cache/hrrr/YYYYMMDD/fNN/<variable>.grib2` (written to a
  `.part` file, then renamed). A cached file is used only if it is one complete GRIB2 message and
  passes the same identity checks as a fresh download; otherwise it is downloaded again.
  A full default run caches about 0.8 GB. Nothing is deleted automatically; `rm -rf .cache` is
  always safe and costs only re-downloads.
- Each forecast hour is inserted in one statement, so an hour is stored completely or not at all.
  A variable that fails (not published, HTTP error, wrong field, missing value) is reported and
  skipped; the other variables of that hour are still stored. Run the same command again to retry
  only what is missing.
- A 403 or 404 on an hour's `.idx` loses that whole hour, not one block, because none of its
  messages can be located. Only those of its messages already in the cache are still stored.
- If a request fails all three attempts (the bucket is down or unreachable), the run makes no
  further requests, but still stores every block whose message is already cached. A failed insert
  (a full disk, a broken database) stops the run, because later inserts would fail the same way.
  Either way the rows already stored stay, and a re-run picks up from there.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Every requested row is in `data.db`. |
| 1 | Something is still missing (a failure was reported), or no complete run was found. |
| 2 | Invalid input. Nothing was downloaded and `data.db` was not touched. |

## Design decisions

The five choices the spec leaves open (D1-D5) are recorded in
[docs/adr/0001-hrrr-ingestion-contract.md](docs/adr/0001-hrrr-ingestion-contract.md):
the meaning of `--num-hours`, what a complete run is, how values and coordinates are stored,
partial failure and exit codes, and the cache.

## Repository tour

| Path | What it holds |
| --- | --- |
| `src/hrrr_ingest/cli.py` | Argument parsing and validation, exit codes, `main()` |
| `src/hrrr_ingest/inventory.py` | The 11-variable registry, S3 paths, `.idx` byte ranges, run completeness |
| `src/hrrr_ingest/source.py` | HTTP GETs and listings against the public bucket, cache file I/O |
| `src/hrrr_ingest/grib.py` | ecCodes decoding, identity and grid checks, nearest grid point |
| `src/hrrr_ingest/store.py` | Table DDL, the missing-key query, the hourly insert |
| `src/hrrr_ingest/ingest.py` | Orchestration of one run, and default-date discovery |
| `tests/` | Offline tests: a fake S3 bucket, tiny real GRIB2 messages, real DuckDB files |
| `tests/fixtures/` | Real HRRR `.idx` files and three real messages (TMP, SFCR, night-time VBDSF) |
| `docs/research/` | What was verified about HRRR, and how |
| `docs/adr/` | Decision records |
| `WRITEUP.md` | Answers to the challenge's write-up questions, and how the build used AI and a rule engine |
| `docs/ai-log.md` | Every prompt given to the AI assistant, logged automatically by a hook (verbatim) |
| `CLAUDE.md` | Working rules given to the AI assistant (Claude Code) for this repository |
| `.llm-rule-engine.json` | Policy for the rule engine that gated every AI change (package allowlist, mutation testing) |
| `.claude/` | Claude Code project settings, the prompt-logging hook, and third-party skills used during the build |

To add an instantaneous variable (GRIB2 product template 0):
- add one `Variable(...)` line to `inventory.VARIABLES` with its `.idx` label and its decoded
  identity (check both with `grib_ls` on a real message);
- add its row to `tests/fixtures/identities.txt`;
- add its name to `SPEC_NAMES` in `tests/test_inventory.py`;
- update the "all 11" in the `--variables` help text in `cli.py`.

An accumulated or averaged field (template 8, such as APCP precipitation) needs code changes, not
just a registry line. The identity check expects template 0 and a single forecast time. And from
f02 on, `APCP:surface` appears twice in each `.idx` file (`0-2 hour acc fcst` and
`1-2 hour acc fcst` at f02), so matching on `VAR:level` alone finds two messages.

## Tests

```bash
uv run pytest
```

All tests are offline. They run the real ecCodes decoder on real HRRR messages and drive `main()`
end to end against a fake S3 bucket that counts every request, with real DuckDB files. Mutation
testing checks that the tests catch injected defects; run it from the project folder:

```bash
uv run mutmut run --CI --paths-to-mutate src/hrrr_ingest/<file>.py
```

Result: 0 surviving mutants in all six modules. The one exclusion is `source._http_get`, the raw
`urllib` call, whose lines are marked `# pragma: no mutate`. The status, range and retry decisions
around it are mutation-tested.

Test names and comments refer to these acceptance tests (T1-T11 offline, T12 manual):

| | What it proves |
| --- | --- |
| T1 | Invalid input exits 2 with no request and no `data.db` change |
| T2 | The table's columns and types match the spec; times are UTC under any local time zone |
| T3 | `.idx` byte ranges are right, and a missing or duplicate label is an error |
| T4 | HTTP rules: 206 + exact Content-Range + length, 404 = not published, bounded retries |
| T5 | Real messages decode as their variable; wrong field, grid or missing value is rejected |
| T6 | Nearest grid point matches ecCodes and a haversine reference |
| T7 | An identical re-run makes 0 requests and changes nothing |
| T8 | Missing rows are found by exact key, not by count |
| T9 | Smaller or bigger re-runs fetch exactly what is new |
| T10 | A failed variable loses only its block, a 403/404 `.idx` its hour; an outage ends requests but cached blocks are still stored; an insert error stops the run; a re-run fetches only what is missing |
| T11 | Bad cache files are replaced; default-date discovery, paging and failures |
| T12 | Manual live check: the spec example twice, values compared with `grib_ls` |

T12 was run on 2026-11-12. The spec example stored 36 rows from 6 requests, and the stored values
matched `grib_ls`. The second run made 0 requests and added 0 rows.

## Docker

```bash
docker build -t hrrr-ingest .
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/data hrrr-ingest points.txt --num-hours 2
```

The container's working directory is the mounted folder, so `points.txt` is read from it and
`data.db` and `.cache/` are written to it on the host. `--user` keeps those files owned by you.
The image is `python:3.12-slim` with the dependencies installed from `uv.lock`; ecCodes ships as a
manylinux wheel for both x86_64 and arm64.

## Measured on a real run

The spec's 9 points with the defaults (all 11 variables, 48 hours, run 2026-11-12 06z), on one
laptop connection:

| | First run | Same command again |
| --- | --- | --- |
| Rows added | 4,752 (9 × 11 × 48) | 0 |
| HTTP requests | 576 (48 `.idx` + 528 messages), plus 1 listing for the default date | 0, plus 1 listing |
| Bytes downloaded | 789 MB | 0 |
| Wall time | 6 min | 1.4 s |
| Cache size | 789 MB (752 MiB) | unchanged |
| Peak memory | about 290 MB | |

The request count in the tool's summary line covers `.idx` files and messages; the one or two
listing calls that find the default date are not included.
