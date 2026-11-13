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
THREE = ("--variables", TWO_VARS, "--num-hours", "3")
F00, F01, F02 = http_url(RUN_DATE, 0), http_url(RUN_DATE, 1), http_url(RUN_DATE, 2)


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
        "no usable cached message for 2025-04-24; downloading one to read the grid",
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
    assert "f01 surface_pressure: wrong field: expected (0, 3, 0, 1, 0, 0, 1, 1, 20250424, 600), " \
        "got (0, 0, 0, 103, 2, 0, 1, 1, 20250424, 600)" in capsys.readouterr().err.splitlines()
    assert not Path(".cache/hrrr/20250424/f01/surface_pressure.grib2").exists()
    s3.add_run(hours=[1])
    s3.calls.clear()
    assert ingest(*ARGS) == 0
    assert s3.calls == [(F01 + ".idx", None, None), message_range(s3, 1, "surface_pressure")]
    assert table() == sorted_expected([5])


def test_an_insert_error_stops_the_run_and_the_rerun_uses_the_cache(s3, monkeypatch, capsys):
    s3.add_run()
    write_points(5)
    real_insert = store.insert

    def insert_failing_at_0700(con, run_time, rows):
        if rows[0][0] == datetime(2025, 4, 24, 7):
            con.execute("SELECT error('disk full')")
        real_insert(con, run_time, rows)

    monkeypatch.setattr(store, "insert", insert_failing_at_0700)
    assert ingest(*THREE) == 1
    assert table() == [r for r in sorted_expected([5]) if r[0] == RUN]
    assert not any(call[0].startswith(F02) for call in s3.calls)  # a full disk would fail f02 too
    assert "f01: insert failed, nothing stored for this hour: Invalid Input Error: disk full; " \
        "stopping the run (a rerun resumes from here)" in capsys.readouterr().err.splitlines()
    monkeypatch.setattr(store, "insert", real_insert)
    s3.calls.clear()
    assert ingest(*THREE) == 0
    assert s3.calls and all(call[0].startswith(F02) for call in s3.calls)  # f01 comes from the cache
    assert table() == sorted_expected([5], hours=(0, 1, 2))


def test_an_unpublished_hour_is_reported_once_and_exits_1(s3, capsys):
    s3.add_run(hours=[0])
    write_points(5)
    assert ingest(*ARGS) == 1
    assert table() == [r for r in sorted_expected([5]) if r[0] == RUN]
    assert s3.calls.count((F01 + ".idx", None, None)) == 1
    err = capsys.readouterr().err.splitlines()
    assert f"f01: not published: {F01}.idx" in err
    sizes = sum(len(s3(*call)) for call in list(s3.calls) if call[0] != F01 + ".idx")
    # The failed .idx request is counted too: 1 + 2 messages + 1 failed .idx = 4.
    assert err[-1] == (
        f"2025-04-24 06z: 1 grid cells, 2 new rows, 4 requests ({sizes:,} bytes), "
        "2 (hour, variable) blocks still missing"
    )


def test_an_unpublished_run_asks_once_stores_nothing_and_exits_1(s3, capsys):
    write_points(5)
    assert ingest(*ARGS) == 1
    assert table() == []
    assert s3.calls == [(F00 + ".idx", None, None)]
    assert capsys.readouterr().err.splitlines() == [
        "no usable cached message for 2025-04-24; downloading one to read the grid",
        f"f00: not published: {F00}.idx",
        "2025-04-24 06z: could not read the grid; nothing stored",
    ]


def test_an_idx_file_with_undecodable_bytes_fails_only_that_hour(s3, capsys):
    s3.add_run()
    s3.objects[F01 + ".idx"] = s3.objects[F01 + ".idx"].replace(b"TMP:2 m", b"TMP:\xff m")
    write_points(5)
    assert ingest("--variables", "temperature_2m", "--num-hours", "2") == 1
    assert table() == [expected_row(0, "temperature_2m", 5)]
    assert "f01 temperature_2m: 'TMP:2 m above ground' appears 0 times in the .idx file" in capsys.readouterr().err.splitlines()


def test_an_unwritable_cache_costs_the_cache_not_the_data(s3, capsys):
    s3.add_run()
    write_points(5)
    Path(".cache").write_text("not a folder")
    assert ingest(*ONE) == 0
    assert table() == [expected_row(0, "temperature_2m", 5)]
    # Nothing can be cached, so the grid bootstrap and the block each download the message once.
    assert len(s3.message_calls()) == 2
    warnings = [line for line in capsys.readouterr().err.splitlines() if "could not cache" in line]
    assert len(warnings) == 2 and len(set(warnings)) == 1
    assert warnings[0].startswith("f00 temperature_2m: could not cache the message (")
    assert warnings[0].endswith("); using it anyway")


def test_a_bad_cached_file_does_not_stop_the_grid_scan_and_is_reported_once(s3, capsys):
    s3.add_run()
    write_points(5)
    f00_only = ("--variables", TWO_VARS, "--num-hours", "1")  # so the only good file is in f00
    assert ingest(*f00_only) == 0
    pres = Path(".cache/hrrr/20250424/f00/surface_pressure.grib2")
    pres.write_bytes(pres.read_bytes()[:-1])  # surface_pressure is scanned before temperature_2m
    with duckdb.connect("data.db") as con:
        con.execute("DELETE FROM hrrr_forecasts WHERE variable = 'surface_pressure'")
    s3.calls.clear()
    capsys.readouterr()
    assert ingest(*f00_only) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "f00 surface_pressure: cached copy rejected (not one complete GRIB2 message); downloading again"
    assert sum("cached copy rejected" in line for line in err) == 1
    assert not any("downloading one to read the grid" in line for line in err)
    assert s3.message_calls() == [message_range(s3, 0, "surface_pressure")]


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
        (lambda m: make_message("surface_pressure"), "wrong field: expected (0, 0, 0, 103, 2, 0, 1, 0, 20250424, 600), got (0, 3, 0, 1, 0, 0, 1, 0, 20250424, 600)"),
    ],
)
def test_a_bad_cached_message_is_downloaded_again(s3, capsys, damage, reason):
    s3.add_run()
    write_points(5)
    assert ingest(*ARGS) == 0
    CACHED_TMP.write_bytes(damage(CACHED_TMP.read_bytes()))
    with duckdb.connect("data.db") as con:
        con.execute("DELETE FROM hrrr_forecasts")
    s3.calls.clear()
    capsys.readouterr()
    assert ingest(*ARGS) == 0
    assert s3.message_calls() == [message_range(s3, 0, "temperature_2m")]
    assert CACHED_TMP.read_bytes() == make_message("temperature_2m")
    err = capsys.readouterr().err.splitlines()
    assert f"f00 temperature_2m: cached copy rejected ({reason}); downloading again" in err
    assert table() == sorted_expected([5])


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
        "no usable cached message for 2025-04-24; downloading one to read the grid",
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


# --- outside review: outages and unusable databases ------------------------------------------------


def test_a_failing_idx_is_fetched_once_and_fails_its_hour_once(s3, capsys):
    s3.add_run()
    denied = source.FetchError(f"HTTP 403 for {F01}.idx")
    s3.failures[F01 + ".idx"] = denied
    write_points(5)
    assert ingest(*THREE) == 1
    assert s3.calls.count((F01 + ".idx", None, None)) == 1
    assert table() == [r for r in sorted_expected([5], hours=(0, 1, 2)) if r[0] != datetime(2025, 4, 24, 7)]
    hour_lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("f01")]
    assert hour_lines == [f"f01: {denied}"]


@pytest.mark.parametrize("target", ["denied idx", "unreachable idx", "unreachable message"])
def test_a_failing_grid_bootstrap_stops_at_once(s3, capsys, target):
    s3.add_run()
    write_points(5)
    url = message_range(s3, 0, "temperature_2m") if target == "unreachable message" else F00 + ".idx"
    error = (source.FetchError if target == "denied idx" else source.Unreachable)(f"HTTP 503 for {F00}, after 3 attempts")
    s3.failures[url] = error
    assert ingest(*ARGS) == 1
    assert s3.calls[-1] == (url if isinstance(url, tuple) else (url, None, None))
    assert len(s3.calls) == (2 if target == "unreachable message" else 1)
    assert capsys.readouterr().err.splitlines() == [
        "no usable cached message for 2025-04-24; downloading one to read the grid",
        f"f00: {error}",
        "2025-04-24 06z: could not read the grid; nothing stored",
    ]


@pytest.mark.parametrize(
    "target, name, kept_at_0700",
    [
        ("idx", "surface_pressure", []),
        ("first message", "surface_pressure", []),
        ("second message", "temperature_2m", ["surface_pressure"]),
    ],
)
def test_an_outage_stops_the_run_keeps_what_it_has_and_a_rerun_resumes(s3, capsys, target, name, kept_at_0700):
    s3.add_run()
    write_points(5)
    url = F01 + ".idx" if target == "idx" else message_range(s3, 1, name)
    outage = source.Unreachable(f"HTTP 503 for {F01}, after 3 attempts")
    s3.failures[url] = outage
    assert ingest(*THREE) == 1
    assert not any(call[0].startswith(F02) for call in s3.calls)
    assert table() == [
        r for r in sorted_expected([5]) if r[0] == RUN or r[4] in kept_at_0700
    ]
    assert f"f01 {name}: {outage}; stopping the run (a rerun resumes from here)" in capsys.readouterr().err.splitlines()
    s3.failures.clear()
    s3.calls.clear()
    assert ingest(*THREE) == 0
    assert not any(call[0].startswith(F00) for call in s3.calls)
    assert table() == sorted_expected([5], hours=(0, 1, 2))


def test_a_404_on_one_message_range_fails_only_that_block(s3, monkeypatch, capsys):
    s3.add_run()
    write_points(5)
    real = s3.__class__.__call__

    def missing_range(self, url, start=None, end=None):
        if (url, start, end) == message_range(self, 1, "surface_pressure"):
            self.calls.append((url, start, end))
            raise source.NotPublished(f"not published: {url}")
        return real(self, url, start, end)

    monkeypatch.setattr(s3.__class__, "__call__", missing_range)
    assert ingest(*ARGS) == 1
    assert [r[4] for r in table() if r[0] == datetime(2025, 4, 24, 7)] == ["temperature_2m"]
    assert f"f01 surface_pressure: not published: {F01}" in capsys.readouterr().err.splitlines()


def unusable(kind):
    path = Path("data.db")
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"this is not a DuckDB database file\n" * 200)
    with pytest.raises(duckdb.Error) as exc:
        duckdb.connect(str(path))
    return str(exc.value).splitlines()[0]


@pytest.mark.parametrize("kind", ["directory", "garbage file"])
def test_an_unusable_database_is_one_line_and_exit_1(s3, capsys, kind):
    s3.add_run()
    write_points(5)
    first_line = unusable(kind)
    assert ingest(*ONE) == 1
    assert capsys.readouterr().err.splitlines() == [f"hrrr-ingest: {first_line}"]


@pytest.mark.parametrize(
    "error",
    [duckdb.IOException("IO Error: disk I/O error\nwhile reading block 7"), OSError(28, "No space left on device")],
)
def test_a_database_or_os_error_mid_run_is_one_line_and_exit_1(s3, monkeypatch, capsys, error):
    s3.add_run()
    write_points(5)

    def failing_missing(*args, **kwargs):
        raise error

    monkeypatch.setattr(store, "missing", failing_missing)
    assert ingest(*ONE) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[-1] == f"hrrr-ingest: {str(error).splitlines()[0]}"
    assert not any("Traceback" in line for line in err)


@pytest.mark.parametrize(
    "error, line", [(OSError(), "OSError"), (OSError("\n"), "OSError"), (duckdb.IOException(""), "IOException")]
)
def test_an_error_with_no_message_is_named_by_its_type(s3, monkeypatch, capsys, error, line):
    s3.add_run()
    write_points(5)

    def failing_missing(*args, **kwargs):
        raise error

    monkeypatch.setattr(store, "missing", failing_missing)
    assert ingest(*ONE) == 1
    assert capsys.readouterr().err.splitlines()[-1] == f"hrrr-ingest: {line}"
