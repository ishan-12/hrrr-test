"""End-to-end through main(argv): fake S3, real GRIB2 decoding, real DuckDB file in a temp folder."""

import shutil
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from conftest import GRID_LATS, GRID_LONS, RUN_DATE, expected_value, make_message, point
from hrrr_ingest import cli, source, store
from hrrr_ingest import ingest as ingest_mod
from hrrr_ingest.cli import main
from hrrr_ingest.inventory import VARIABLES, byte_range, http_url

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


ARGS = ("--variables", TWO_VARS, "--num-hours", "2")
F00, F01 = http_url(RUN_DATE, 0), http_url(RUN_DATE, 1)


def message_range(s3, hour, name):
    idx = s3.objects[http_url(RUN_DATE, hour) + ".idx"].decode()
    return (http_url(RUN_DATE, hour), *byte_range(idx, VARIABLES[name]))


def sorted_expected(cells, hours=(0, 1), names=("surface_pressure", "temperature_2m")):
    keys = sorted((h, GRID_LATS[c], GRID_LONS[c], n, c) for h in hours for n in names for c in cells)
    return [expected_row(h, n, c) for h, _, _, n, c in keys]


# --- T1: invalid input through main -----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["missing.txt"],
        ["points.txt", "--num-hours", "49"],
        ["points.txt", "--variables", "temperature"],
        ["points.txt", "--run-date", "2999-01-01"],
    ],
)
def test_invalid_input_exits_2_with_no_requests_and_no_database(s3, argv):
    write_points(5)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert s3.calls == []
    assert not Path("data.db").exists()


# --- T2: the stored table ---------------------------------------------------------------------


def test_rows_and_times_are_utc_even_under_another_local_time_zone(s3, utc_tz):
    s3.add_run()
    write_points(5)
    assert ingest(*ARGS) == 0
    assert table() == sorted_expected([5])
    assert {row[6] for row in table() if row[0] == RUN} == {SPEC_SOURCE_F00}
    with duckdb.connect("data.db", read_only=True) as con:
        assert [r[:2] for r in con.execute("DESCRIBE hrrr_forecasts").fetchall()] == [
            ("valid_time_utc", "TIMESTAMP"),
            ("run_time_utc", "TIMESTAMP"),
            ("latitude", "FLOAT"),
            ("longitude", "FLOAT"),
            ("variable", "VARCHAR"),
            ("value", "FLOAT"),
            ("source_s3", "VARCHAR"),
        ]


def test_first_run_fetches_each_index_once_and_each_message_once(s3, capsys):
    s3.add_run()
    write_points(5, 10)
    assert ingest(*ARGS) == 0
    assert s3.calls == [
        (F00 + ".idx", None, None),
        message_range(s3, 0, "temperature_2m"),  # the grid bootstrap, reused for its block
        message_range(s3, 0, "surface_pressure"),
        (F01 + ".idx", None, None),
        message_range(s3, 1, "surface_pressure"),
        message_range(s3, 1, "temperature_2m"),
    ]
    sizes = sum(len(s3(*call)) for call in list(s3.calls))
    assert capsys.readouterr().err.splitlines() == [
        "no cached message for 2025-04-24; downloading one to read the grid",
        f"2025-04-24 06z: 2 grid cells, 8 new rows, 6 requests ({sizes:,} bytes), "
        "0 (hour, variable) blocks still missing",
    ]


def test_two_points_in_one_grid_cell_give_one_set_of_rows(s3):
    s3.add_run()
    lat, lon = point(5)
    write_points(5, extra=[(lat + 0.001, lon - 0.001)])
    assert ingest(*ARGS) == 0
    assert table() == sorted_expected([5])


def test_far_points_are_kept_with_a_warning(s3, capsys):
    s3.add_run()
    # South of the test grid's first row: cell 0 is the nearest point either way.
    lat, lon = point(0)
    # About 3.5 km, 3.02 km (printed as 3.0, so no warning) and 2.5 km away.
    write_points(extra=[(lat - 0.0315, lon), (lat - 0.0271, lon), (lat - 0.0225, lon)])
    assert ingest(*ARGS) == 0
    assert table() == sorted_expected([0])
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("warning")]
    assert warnings == [f"warning: point {lat - 0.0315},{lon} is 3.5 km from the nearest grid point"]


# --- T7: identical re-run ------------------------------------------------------------------------


def test_identical_rerun_makes_no_requests_and_changes_nothing(s3, capsys):
    s3.add_run()
    write_points(5, 10)
    assert ingest(*ARGS) == 0
    before = table()
    s3.calls.clear()
    capsys.readouterr()
    assert ingest(*ARGS) == 0
    assert s3.calls == []
    assert table() == before
    assert capsys.readouterr().err.splitlines() == [
        "2025-04-24 06z: 2 grid cells, 0 new rows, 0 requests (0 bytes), 0 (hour, variable) blocks still missing"
    ]


# --- T8: equal counts, different cells --------------------------------------------------------------


def test_row_counts_follow_cells_not_point_counts(s3):
    s3.add_run()
    for cells, expected in [((0, 1), [0, 1]), ((2, 3), [0, 1, 2, 3]), ((1, 4), [0, 1, 2, 3, 4])]:
        write_points(*cells)
        assert ingest(*ARGS) == 0
        assert table() == sorted_expected(expected)
    assert len(table()) == 20


def test_a_deleted_row_is_restored_even_when_an_extra_row_keeps_the_count(s3):
    s3.add_run()
    write_points(5, 10)
    assert ingest(*ARGS) == 0
    full = table()
    with duckdb.connect("data.db") as con:
        con.execute(
            "DELETE FROM hrrr_forecasts WHERE variable = 'temperature_2m' "
            "AND valid_time_utc = TIMESTAMP '2025-04-24 07:00:00' AND latitude > 21.19"
        )
        con.execute(
            "INSERT INTO hrrr_forecasts VALUES (TIMESTAMP '2025-04-24 07:00:00', TIMESTAMP '2025-04-24 06:00:00', "
            "40.0, -100.0, 'temperature_2m', 1.0, 'unrelated')"
        )
        assert con.execute("SELECT count(*) FROM hrrr_forecasts").fetchone() == (len(full),)
    s3.calls.clear()
    assert ingest(*ARGS) == 0
    assert s3.calls == []  # restored from the cache
    assert [row for row in table() if row[6] != "unrelated"] == full


# --- T9: smaller and bigger re-runs -----------------------------------------------------------------


def test_smaller_rerun_makes_no_requests(s3):
    s3.add_run()
    write_points(5, 10)
    assert ingest(*ARGS) == 0
    s3.calls.clear()
    assert ingest("--variables", "temperature_2m", "--num-hours", "1") == 0
    write_points(10)
    assert ingest(*ARGS) == 0
    assert s3.calls == []


def test_added_hours_and_variables_fetch_exactly_the_new_blocks(s3):
    s3.add_run()
    write_points(5)
    assert ingest("--variables", "temperature_2m", "--num-hours", "1") == 0
    s3.calls.clear()
    assert ingest("--variables", "temperature_2m,dewpoint_2m", "--num-hours", "3") == 0
    assert s3.calls == [
        (F00 + ".idx", None, None),
        message_range(s3, 0, "dewpoint_2m"),
        (F01 + ".idx", None, None),
        message_range(s3, 1, "dewpoint_2m"),
        message_range(s3, 1, "temperature_2m"),
        (http_url(RUN_DATE, 2) + ".idx", None, None),
        message_range(s3, 2, "dewpoint_2m"),
        message_range(s3, 2, "temperature_2m"),
    ]
    assert len(table()) == 6


def test_added_point_uses_the_cache_and_keeps_old_rows(s3):
    s3.add_run()
    write_points(5)
    assert ingest(*ARGS) == 0
    old = table()
    s3.calls.clear()
    write_points(5, 10)
    assert ingest(*ARGS) == 0
    assert s3.calls == []
    assert table() == sorted_expected([5, 10])
    assert set(old) <= set(table())


# --- T10: failures ----------------------------------------------------------------------------------


def test_one_bad_variable_keeps_the_rest_of_its_hour_and_a_rerun_fetches_only_it(s3, capsys):
    s3.add_run()
    pres, tmp = VARIABLES["surface_pressure"], VARIABLES["temperature_2m"]
    wrong = make_message("temperature_2m", RUN_DATE, 1)
    s3.add_hour(RUN_DATE, 1, {pres.idx_label: wrong, tmp.idx_label: make_message("temperature_2m", RUN_DATE, 1)})
    write_points(5)
    assert ingest(*ARGS) == 1
    assert table() == [r for r in sorted_expected([5]) if not (r[0] == datetime(2025, 4, 24, 7) and r[4] == "surface_pressure")]
    assert "f01 surface_pressure: wrong field: expected (0, 3, 0, 1, 0, 1, 1, 20250424, 600), " \
        "got (0, 0, 0, 103, 2, 1, 1, 20250424, 600)" in capsys.readouterr().err.splitlines()
    assert not Path(".cache/hrrr/20250424/f01/surface_pressure.grib2").exists()
    s3.add_run(hours=[1])
    s3.calls.clear()
    assert ingest(*ARGS) == 0
    assert s3.calls == [(F01 + ".idx", None, None), message_range(s3, 1, "surface_pressure")]
    assert table() == sorted_expected([5])


def test_insert_error_loses_only_that_hour_and_the_rerun_uses_the_cache(s3, monkeypatch, capsys):
    s3.add_run()
    write_points(5)
    real_insert = store.insert

    def insert_failing_at_0700(con, run_time, rows):
        if rows[0][0] == datetime(2025, 4, 24, 7):
            con.execute("SELECT error('disk full')")
        real_insert(con, run_time, rows)

    monkeypatch.setattr(store, "insert", insert_failing_at_0700)
    assert ingest(*ARGS) == 1
    assert table() == [r for r in sorted_expected([5]) if r[0] == RUN]
    assert any(line.startswith("f01: insert failed, nothing stored for this hour: ") for line in capsys.readouterr().err.splitlines())
    monkeypatch.setattr(store, "insert", real_insert)
    s3.calls.clear()
    assert ingest(*ARGS) == 0
    assert s3.calls == []
    assert table() == sorted_expected([5])


def test_an_unpublished_hour_is_reported_once_and_exits_1(s3, capsys):
    s3.add_run(hours=[0])
    write_points(5)
    assert ingest(*ARGS) == 1
    assert table() == [r for r in sorted_expected([5]) if r[0] == RUN]
    assert s3.calls.count((F01 + ".idx", None, None)) == 1
    assert f"f01: not published: {F01}.idx" in capsys.readouterr().err.splitlines()


def test_an_unpublished_run_stores_nothing_and_exits_1(s3, capsys):
    write_points(5)
    assert ingest(*ARGS) == 1
    assert table() == []
    err = capsys.readouterr().err.splitlines()
    assert err[-1] == "2025-04-24 06z: could not read the grid; nothing stored"
    assert f"f00 temperature_2m: not published: {F00}.idx" in err


def test_the_grid_bootstrap_tries_the_next_variable(s3):
    s3.add_hour(RUN_DATE, 0, {VARIABLES["surface_pressure"].idx_label: make_message("surface_pressure")})
    s3.add_hour(RUN_DATE, 1, {VARIABLES["surface_pressure"].idx_label: make_message("surface_pressure", RUN_DATE, 1)})
    write_points(5)
    assert ingest(*ARGS) == 1  # temperature_2m is not in these files
    assert [r[4] for r in table()] == ["surface_pressure", "surface_pressure"]


# --- T11: cache and default date ------------------------------------------------------------------

CACHED_TMP = Path(".cache/hrrr/20250424/f00/temperature_2m.grib2")
ONE = ("--variables", "temperature_2m", "--num-hours", "1")


def test_messages_are_cached_under_date_hour_and_name(s3):
    s3.add_run()
    write_points(5)
    assert ingest(*ARGS) == 0
    cached = sorted(str(p) for p in Path(".cache").rglob("*"))
    assert cached == [
        ".cache/hrrr",
        ".cache/hrrr/20250424",
        ".cache/hrrr/20250424/f00",
        ".cache/hrrr/20250424/f00/surface_pressure.grib2",
        ".cache/hrrr/20250424/f00/temperature_2m.grib2",
        ".cache/hrrr/20250424/f01",
        ".cache/hrrr/20250424/f01/surface_pressure.grib2",
        ".cache/hrrr/20250424/f01/temperature_2m.grib2",
    ]
    assert CACHED_TMP.read_bytes() == make_message("temperature_2m")


@pytest.mark.parametrize(
    "damage, reason",
    [
        (lambda m: m[:-1], "not one complete GRIB2 message"),
        (lambda m: make_message("surface_pressure"), "wrong field: expected (0, 0, 0, 103, 2, 1, 0, 20250424, 600), got (0, 3, 0, 1, 0, 1, 0, 20250424, 600)"),
    ],
)
def test_a_bad_cached_message_is_downloaded_again(s3, capsys, damage, reason):
    s3.add_run()
    write_points(5)
    assert ingest(*ONE) == 0
    CACHED_TMP.write_bytes(damage(CACHED_TMP.read_bytes()))
    with duckdb.connect("data.db") as con:
        con.execute("DELETE FROM hrrr_forecasts")
    s3.calls.clear()
    capsys.readouterr()
    assert ingest(*ONE) == 0
    assert s3.message_calls() == [message_range(s3, 0, "temperature_2m")]
    assert CACHED_TMP.read_bytes() == make_message("temperature_2m")
    err = capsys.readouterr().err.splitlines()
    assert f"f00 temperature_2m: cached copy rejected ({reason}); downloading again" in err
    assert table() == [expected_row(0, "temperature_2m", 5)]


def test_a_leftover_partial_download_is_ignored_and_replaced(s3):
    s3.add_run()
    write_points(5)
    partial = CACHED_TMP.with_name("temperature_2m.grib2.part")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(make_message("temperature_2m")[:50])
    assert ingest(*ONE) == 0
    assert not partial.exists()
    assert CACHED_TMP.read_bytes() == make_message("temperature_2m")


def test_a_message_cut_off_mid_body_is_not_cached(s3, capsys):
    s3.add_run()
    write_points(5)
    s3.objects[F00] = s3.objects[F00][:100]  # every message range now comes back short
    assert ingest(*ONE) == 1
    assert not CACHED_TMP.exists()
    assert table() == []


def test_a_deleted_cache_costs_one_bootstrap_download_and_no_rows(s3, capsys):
    s3.add_run()
    write_points(5)
    assert ingest(*ARGS) == 0
    before = table()
    shutil.rmtree(".cache")
    s3.calls.clear()
    capsys.readouterr()
    assert ingest(*ARGS) == 0
    assert s3.calls == [(F00 + ".idx", None, None), message_range(s3, 0, "temperature_2m")]
    assert table() == before
    sizes = sum(len(s3(*call)) for call in list(s3.calls))
    assert capsys.readouterr().err.splitlines() == [
        "no cached message for 2025-04-24; downloading one to read the grid",
        f"2025-04-24 06z: 1 grid cells, 0 new rows, 2 requests ({sizes:,} bytes), "
        "0 (hour, variable) blocks still missing",
    ]


@pytest.mark.parametrize("hour, bootstraps", [(48, False), (49, True)])
def test_the_grid_comes_from_any_cached_hour_from_f00_to_f48(s3, capsys, hour, bootstraps):
    s3.add_run()
    write_points(5)
    path = Path(f".cache/hrrr/20250424/f{hour}/temperature_2m.grib2")
    path.parent.mkdir(parents=True)
    path.write_bytes(make_message("temperature_2m", RUN_DATE, hour))
    assert ingest(*ONE) == 0
    assert s3.calls == [(F00 + ".idx", None, None), message_range(s3, 0, "temperature_2m")]
    assert ("downloading one to read the grid" in capsys.readouterr().err) is bootstraps


def test_default_date_skips_an_incomplete_newest_date_and_reads_paged_listings(s3, monkeypatch):
    monkeypatch.setattr(cli, "utc_today", lambda: date(2025, 4, 25))
    s3.add_run()
    s3.add_listing(RUN_DATE)
    s3.add_listing(date(2025, 4, 25), hours=range(46))  # half-published
    s3.page_size = 40
    write_points(5)
    assert main(["points.txt", *ONE]) == 0
    listings = [call[0] for call in s3.calls if "?" in call[0]]
    assert len(listings) == 6  # 92 keys for 04-25 and 98 for 04-24, 40 per page
    assert {row[1] for row in table()} == {RUN}


def test_default_date_just_after_utc_midnight_falls_back_a_day(s3):
    s3.add_listing(RUN_DATE)
    assert ingest_mod.latest_complete_date(date(2025, 4, 25), s3) == RUN_DATE
    assert [c[0].split("prefix=")[1] for c in s3.calls] == [
        "hrrr.20250425%2Fconus%2Fhrrr.t06z.wrfsfcf",
        "hrrr.20250424%2Fconus%2Fhrrr.t06z.wrfsfcf",
    ]


def test_default_date_stops_at_2020_12_03(s3, monkeypatch, capsys):
    s3.add_listing(date(2020, 12, 3))
    assert ingest_mod.latest_complete_date(date(2020, 12, 4), s3) == date(2020, 12, 3)
    s3.objects.clear()
    s3.calls.clear()
    monkeypatch.setattr(cli, "utc_today", lambda: date(2020, 12, 5))
    write_points(5)
    assert main(["points.txt"]) == 1
    assert len(s3.calls) == 3  # 12-05, 12-04, 12-03
    assert capsys.readouterr().err.strip() == (
        "hrrr-ingest: cannot find the latest complete run: no complete 06z run between 2020-12-03 and 2020-12-05"
    )
    assert not Path("data.db").exists()


@pytest.mark.parametrize("error", [source.FetchError("HTTP 403 for https://x"), source.FetchError("TimeoutError: t")])
def test_a_listing_failure_is_exit_1_not_an_incomplete_run(s3, monkeypatch, capsys, error):
    def failing(url, start=None, end=None):
        s3.calls.append((url, start, end))
        raise error

    monkeypatch.setattr(source, "fetch", failing)
    write_points(5)
    assert main(["points.txt"]) == 1
    assert len(s3.calls) == 1
    assert capsys.readouterr().err.strip() == f"hrrr-ingest: cannot find the latest complete run: {error}"


def test_an_explicit_date_makes_no_listing_call(s3):
    s3.add_run()
    write_points(5)
    assert ingest(*ONE) == 0
    assert s3.calls and not any("?" in call[0] for call in s3.calls)
