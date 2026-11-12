"""T2 (schema half) and T8 (exact-key detection) against a real temporary DuckDB file."""

from datetime import datetime

import duckdb
import pytest

from hrrr_ingest import store

RUN = datetime(2025, 4, 24, 6)
F00, F01 = datetime(2025, 4, 24, 6), datetime(2025, 4, 24, 7)
SRC = "s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250424/conus/hrrr.t06z.wrfsfcf00.grib2"
# Not exactly representable as float32: matching only works if both sides round the same way.
LAT, LON = 31.006900787353516 + 1e-9, -88.01030731201172 - 1e-9


@pytest.fixture
def con(tmp_path):
    con = store.connect(str(tmp_path / "data.db"))
    yield con
    con.close()


def test_schema_matches_the_spec(con):
    columns = [(row[0], row[1]) for row in con.execute("DESCRIBE hrrr_forecasts").fetchall()]
    assert columns == [
        ("valid_time_utc", "TIMESTAMP"),
        ("run_time_utc", "TIMESTAMP"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
        ("variable", "VARCHAR"),
        ("value", "FLOAT"),
        ("source_s3", "VARCHAR"),
    ]


def test_connect_twice_keeps_rows(tmp_path):
    path = str(tmp_path / "data.db")
    with store.connect(path) as con:
        store.insert(con, RUN, [(F00, LAT, LON, "temperature_2m", 290.5, SRC)])
    with store.connect(path) as con:
        assert con.execute("SELECT count(*) FROM hrrr_forecasts").fetchone() == (1,)


def test_insert_round_trip(con):
    store.insert(con, RUN, [(F01, LAT, LON, "temperature_2m", 0.0, SRC)])
    assert con.execute("SELECT * FROM hrrr_forecasts").fetchall() == [
        (F01, RUN, pytest.approx(LAT, abs=1e-5), pytest.approx(LON, abs=1e-5), "temperature_2m", 0.0, SRC)
    ]


def test_missing_is_exact_per_key_with_float32_coordinates(con):
    keys = [(t, v, lat, LON) for t in (F00, F01) for v in ("a", "b") for lat in (LAT, 32.5)]
    assert store.missing(con, RUN, keys) == [(F00, "a"), (F00, "b"), (F01, "a"), (F01, "b")]
    store.insert(con, RUN, [(F00, LAT, LON, "a", 1.0, SRC), (F00, 32.5, LON, "a", 2.0, SRC)])
    store.insert(con, RUN, [(F01, LAT, LON, "b", 3.0, SRC)])
    assert store.missing(con, RUN, keys) == [(F00, "b"), (F01, "a"), (F01, "b")]


@pytest.mark.parametrize(
    "stored",
    [
        (datetime(2025, 4, 23, 6), F00, LAT, LON, "a"),  # other run
        (RUN, F01, LAT, LON, "a"),  # other valid time
        (RUN, F00, LAT, LON, "b"),  # other variable
        (RUN, F00, 32.5, LON, "a"),  # other latitude
        (RUN, F00, LAT, -86.0, "a"),  # other longitude
    ],
)
def test_a_row_differing_in_any_key_column_does_not_count(con, stored):
    run, valid, lat, lon, var = stored
    store.insert(con, run, [(valid, lat, lon, var, 1.0, SRC)])
    assert store.missing(con, RUN, [(F00, "a", LAT, LON)]) == [(F00, "a")]


def test_insert_never_overwrites_or_duplicates(con):
    store.insert(con, RUN, [(F00, LAT, LON, "a", 1.0, SRC)])
    store.insert(con, RUN, [(F00, LAT, LON, "a", 99.0, SRC), (F01, LAT, LON, "a", 2.0, SRC)])
    assert con.execute("SELECT valid_time_utc, value FROM hrrr_forecasts ORDER BY 1").fetchall() == [
        (F00, 1.0),
        (F01, 2.0),
    ]


def test_failed_insert_stores_nothing_from_that_hour(con):
    with pytest.raises(duckdb.Error):
        store.insert(con, RUN, [(F00, LAT, LON, "a", 1.0, SRC), (F00, LAT, LON, "b", None, SRC)])
    assert con.execute("SELECT count(*) FROM hrrr_forecasts").fetchone() == (0,)
