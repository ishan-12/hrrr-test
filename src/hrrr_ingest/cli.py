"""The hrrr-ingest command: argument parsing and validation.

Exit codes: 0 every requested row is stored, 1 something is missing or failed, 2 invalid input.
Invalid input is rejected here, before any network request or database change.
"""

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from hrrr_ingest import ingest, source, store
from hrrr_ingest.inventory import LAST_HOUR, VARIABLES

MAX_HOURS = LAST_HOUR  # --num-hours N ingests f00 to f(N-1), so at most f47 (ADR D1)
DB_PATH = "data.db"
CACHE_DIR = Path(".cache/hrrr")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _points(path: str) -> list[tuple[float, float]]:
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise argparse.ArgumentTypeError(f"cannot read {path}: {e.strerror}")
    points = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            lat, lon = (float(part) for part in line.split(","))
        except ValueError:
            raise argparse.ArgumentTypeError(f"line {n}: expected 'latitude,longitude', got {line!r}")
        # Written so that NaN fails every comparison and is rejected too.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise argparse.ArgumentTypeError(f"line {n}: {line!r} is not a valid latitude,longitude")
        points.append((lat, lon))
    if not points:
        raise argparse.ArgumentTypeError(f"{path} has no points")
    return list(dict.fromkeys(points))


def _run_date(text: str) -> date:
    try:
        run_date = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    if run_date > utc_today():
        raise argparse.ArgumentTypeError(f"{run_date} is in the future (UTC)")
    return run_date


def _variables(text: str) -> list:
    names = [name.strip() for name in text.split(",") if name.strip()]
    unknown = [name for name in names if name not in VARIABLES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(
            f"unknown or empty: {text!r}; choose from {', '.join(VARIABLES)}"
        )
    return [VARIABLES[name] for name in dict.fromkeys(names)]


def _num_hours(text: str) -> int:
    try:
        hours = int(text)
    except ValueError:
        hours = 0
    if not 1 <= hours <= MAX_HOURS:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_HOURS}, got {text!r}")
    return hours


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hrrr-ingest",
        description="Ingest NOAA HRRR 06z forecasts for a list of points into data.db (DuckDB).",
    )
    parser.add_argument("points", type=_points, help="text file with one 'latitude,longitude' per line")
    parser.add_argument(
        "--run-date",
        type=_run_date,
        help="06z run date, YYYY-MM-DD (default: the latest UTC date whose 06z run is complete)",
    )
    parser.add_argument(
        "--variables",
        type=_variables,
        default=list(VARIABLES.values()),
        help="comma-separated variable names (default: all 11)",
    )
    parser.add_argument(
        "--num-hours",
        type=_num_hours,
        default=MAX_HOURS,
        help="forecast hours to ingest, f00 to f(N-1), 1-48 (default: 48)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_date = args.run_date or ingest.latest_complete_date(utc_today(), source.fetch)
    except (source.FetchError, LookupError) as e:
        print(f"hrrr-ingest: cannot find the latest complete run: {e}", file=sys.stderr)
        return 1
    try:
        with store.connect(DB_PATH) as con:
            complete = ingest.ingest(
                con, run_date, range(args.num_hours), args.variables, args.points, CACHE_DIR, source.fetch
            )
    except (duckdb.Error, OSError) as e:  # locked, unusable or unwritable data.db, a full disk, ...
        print(f"hrrr-ingest: {ingest.first_line(e)}", file=sys.stderr)
        return 1
    return 0 if complete else 1


def run() -> None:
    sys.exit(main())
