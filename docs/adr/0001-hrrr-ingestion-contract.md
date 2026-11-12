# ADR-0001: HRRR ingestion contract (D1-D5)

## Status

Accepted. Drafted by Claude; owner's wording pending.

## Date

2026-11-12

## Context

The challenge spec (`comity-coding-challenge-weather.md`) fixes the CLI flags, the `hrrr_forecasts`
schema, the 11 variable names and the `source_s3` format. It leaves five behaviours open. Each one
changes which rows end up in `data.db` or what a re-run does, so each is settled here.

## Decision

**D1. `--num-hours N` means forecast hours f00 to f(N-1), with 1 <= N <= 48.**
`--num-hours 2` stores 06:00 and 07:00 UTC. The default (48) stores f00-f47. The spec's own example
path ends in `wrfsfcf00`, so f00 is included. N outside 1-48 is invalid input.

**D2. A run is complete when all 49 GRIB2 + `.idx` pairs (f00-f48) are listed in the bucket.**
The default `--run-date` is the newest UTC date whose 06z run is complete. Discovery walks back one
UTC day at a time from today and stops at 2020-12-03, the first 06z run with 49 hours. The test
does not depend on N, so changing `--num-hours` never moves the default date. A network error,
403 or malformed listing is a failure (exit 1), never "incomplete". An explicit `--run-date` makes
no listing call.

**D3. Values and coordinates are stored as published.**
- Native GRIB units (K, Pa, %, m, m/s, W/m^2); no conversion.
- Winds are the native grid-relative u/v components (`resolutionAndComponentFlags` = 8), not rotated
  to true east/north.
- `latitude`/`longitude` are the chosen grid point's coordinates (not the input point's), as float32,
  with longitude in -180..180.
- One key per grid cell: two input points that snap to the same cell produce one set of rows.
- A missing or non-finite value is never stored, and never replaced by zero. A real zero (night-time
  solar flux) is stored as zero.
- Every input point is accepted; a point more than 3 km from its grid point gets a warning.

**D4. Partial failure: one transaction per forecast hour.**
The variable blocks that decode and pass every check for an hour are committed together. A block
that fails (not published, HTTP error, identity mismatch, missing value) is reported and skipped;
an insert error rolls back that hour only. Exit codes:
- 0 when every requested key is in the table at the end of the run;
- 1 when anything is still missing or failed;
- 2 for invalid input, detected before any network call or database change.

**D5. Downloaded messages are cached in `.cache/hrrr/` under the working directory.**
One file per message (`YYYYMMDD/fNN/<variable>.grib2`), written to a temporary file and renamed into
place. A cached file is reused only if it is a complete GRIB2 message (its length matches the
length in its own header and it ends in `7777`) and it passes the same identity checks as a fresh
download; otherwise it is downloaded again and the new copy replaces it. If the cache cannot be
written (full or read-only disk), the downloaded message is still used and the failure reported. There is no automatic clean-up; a full default
run caches about 0.8 GB. Deleting the folder is always safe.

## Alternatives considered

- **N hours meaning f01-fN, or N+1 files.** Rejected: it drops the analysis hour that the spec's own
  example path names.
- **"Complete" meaning only the N requested hours exist.** Rejected: the default date would move
  with `--num-hours`, so a test run and a full run could land on different dates.
- **Rotating winds to earth-relative.** Deferred: it changes the published values, and every
  common tool (ecCodes, cfgrib, Herbie) returns grid-relative components.
- **Rejecting distant points.** Rejected: the spec asks for the closest available point.
- **All-or-nothing per run.** Rejected: one bad message would discard 47 good hours and force them
  to be downloaded again.
- **No cache.** Rejected: the spec asks the tool to skip data that is already downloaded, and an
  added point would otherwise re-download every message.

## Consequences

- Re-running the same command with an explicit date and an intact cache makes no network requests
  and adds no rows.
- The default date costs one or two listing requests per run.
- Users who want earth-relative winds must rotate them downstream; the README says so.
- The cache directory grows without bound across run dates until deleted by hand.
