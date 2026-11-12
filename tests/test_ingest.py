"""End-to-end through main(argv): fake S3, real GRIB2 decoding, real DuckDB file in a temp folder."""

from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from conftest import GRID_LATS, GRID_LONS, RUN_DATE, expected_value, make_message, point
from hrrr_ingest import store
from hrrr_ingest.cli import main
from hrrr_ingest.inventory import VARIABLES, http_url

RUN = datetime(2025, 4, 24, 6)
SPEC_SOURCE_F00 = "s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250424/conus/hrrr.t06z.wrfsfcf00.grib2"
TWO_VARS = "temperature_2m,surface_pressure"


def write_points(*cells, extra=()):
    lines = [f"{lat},{lon}" for lat, lon in [*map(point, cells), *extra]]
    Path("points.txt").write_text("\n".join(lines) + "\n")


def ingest(*flags, date="2025-04-24"):
    return main(["points.txt", "--run-date", date, *flags])


def table():
    with duckdb.connect("data.db", read_only=True) as con:
        return con.execute("SELECT * FROM hrrr_forecasts ORDER BY ALL").fetchall()


def expected_row(hour, name, cell):
    source = f"s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250424/conus/hrrr.t06z.wrfsfcf{hour:02d}.grib2"
    return (
        datetime(2025, 4, 24, 6 + hour),
        RUN,
        pytest.approx(GRID_LATS[cell], abs=2e-6),
        pytest.approx(GRID_LONS[cell] - 360, abs=2e-5),
        name,
        pytest.approx(expected_value(name, hour, cell), abs=1e-3),
        source,
    )


def test_vertical_slice_one_variable_one_hour_two_points(s3):
    s3.add_run(hours=range(1), names=["temperature_2m"])
    write_points(5, 10)
    assert ingest("--variables", "temperature_2m", "--num-hours", "1") == 0
    assert table() == [expected_row(0, "temperature_2m", 5), expected_row(0, "temperature_2m", 10)]
    assert table()[0][6] == SPEC_SOURCE_F00
