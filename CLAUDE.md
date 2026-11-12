# CLAUDE.md

## The task

`hrrr-ingest`: a Python CLI that ingests NOAA HRRR GRIB2 forecasts (the 06z run, up to 48 hours) for a
list of lat/lon points into DuckDB (`data.db`, table `hrrr_forecasts`, long format). The full spec is
in [comity-coding-challenge-weather.md](comity-coding-challenge-weather.md). Graded in this order:
correctness (including idempotency), clarity, adaptability, bonus features.

The spec is the source of truth for the table schema, the 11 stored variable names, the CLI flags
and the `source_s3` path format. Don't rename or reshape any of them.

## Working rules

1. **Say what you assume.** If the spec or the data is ambiguous, name the ambiguity and ask. Don't
   pick an interpretation silently. HRRR variable names, grid levels and the "last available complete
   run" default are known traps.
2. **Write the smallest code that solves it.** No options, layers or abstractions the spec doesn't
   need. If it could be half the length, make it half the length.
3. **Keep changes surgical.** Touch only what the task needs. Mention unrelated problems; don't fix
   them unasked.
4. **Define done as something checkable.** Before coding, state the check that proves the step works
   (a test, a query against `data.db`, a second run that adds no rows), then run it.
5. **Verify facts about HRRR against primary sources.** Use NCEP's inventory tables, the `.idx`
   files and `grib_ls` output, not memory. Record what you verified in `docs/research/`.

Adapted from the guidelines in
[multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills).

## Project conventions

- Record decisions as ADRs in `docs/adr/` (see the `documentation-and-adrs` skill).
- Tests run with `uv run pytest`. Tests use small fixture files, never live S3.
- Every user prompt is logged automatically to `docs/ai-log.md` for the write-up. Don't edit that
  file by hand.
- `llm-rule-engine` gates code changes in this repo; its config is `.llm-rule-engine.json`.
