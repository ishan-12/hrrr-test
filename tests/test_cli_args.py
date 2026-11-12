"""T1 (argument half): invalid input is rejected by the parser with exit code 2."""

from datetime import timedelta

import pytest

from hrrr_ingest.cli import build_parser, utc_today
from hrrr_ingest.inventory import VARIABLES


def parse(tmp_path, points_text="31.0069,-88.0103\n", *flags):
    path = tmp_path / "points.txt"
    path.write_text(points_text)
    return build_parser().parse_args([str(path), *flags])


def error(capsys, tmp_path, points_text="31.0069,-88.0103\n", *flags):
    with pytest.raises(SystemExit) as exc:
        parse(tmp_path, points_text, *flags)
    assert exc.value.code == 2
    return capsys.readouterr().err.splitlines()[-1].removeprefix("hrrr-ingest: error: ")


def test_defaults(tmp_path):
    args = parse(tmp_path, "31.0069,-88.0103\n\n  \n32.5,-86.2\n")
    assert args.points == [(31.0069, -88.0103), (32.5, -86.2)]
    assert args.run_date is None
    assert args.variables == list(VARIABLES.values())
    assert args.num_hours == 48


def test_duplicates_collapse(tmp_path):
    args = parse(
        tmp_path,
        "31.0,-88.0\n31.0, -88.0\n",
        "--variables",
        " temperature_2m,surface_pressure,temperature_2m,",
    )
    assert args.points == [(31.0, -88.0)]
    assert [v.name for v in args.variables] == ["temperature_2m", "surface_pressure"]


@pytest.mark.parametrize("line", ["90,180", "-90,-180", "0,0"])
def test_coordinate_bounds_are_inclusive(tmp_path, line):
    assert len(parse(tmp_path, line).points) == 1


@pytest.mark.parametrize(
    "line",
    ["90.001,0", "-90.001,0", "0,180.001", "0,-180.001", "nan,0", "0,nan", "inf,0", "0,-inf", "1e999,0"],
)
def test_out_of_range_or_non_finite_points(capsys, tmp_path, line):
    message = error(capsys, tmp_path, f"31,-88\n{line}\n")
    assert message == f"argument points: line 2: {line!r} is not a valid latitude,longitude"


@pytest.mark.parametrize("line", ["abc,0", "1,2,3", "31.5", ""])
def test_malformed_point_lines(capsys, tmp_path, line):
    message = error(capsys, tmp_path, f"{line};\n")
    assert message == f"argument points: line 1: expected 'latitude,longitude', got '{line};'"


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_empty_points_file(capsys, tmp_path, text):
    assert error(capsys, tmp_path, text) == f"argument points: {tmp_path / 'points.txt'} has no points"


def test_missing_points_file(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([str(tmp_path / "nope.txt")])
    assert exc.value.code == 2
    last = capsys.readouterr().err.splitlines()[-1]
    assert last == f"hrrr-ingest: error: argument points: cannot read {tmp_path / 'nope.txt'}: No such file or directory"


def test_run_date_today_is_allowed_tomorrow_is_not(capsys, tmp_path):
    today = utc_today()
    assert parse(tmp_path, "31,-88", "--run-date", today.isoformat()).run_date == today
    tomorrow = today + timedelta(days=1)
    message = error(capsys, tmp_path, "31,-88", "--run-date", tomorrow.isoformat())
    assert message == f"argument --run-date: {tomorrow} is in the future (UTC)"


@pytest.mark.parametrize("text", ["2025-13-01", "yesterday", "", "20250424", "2025-W17-4"])
def test_bad_run_date(capsys, tmp_path, text):
    assert error(capsys, tmp_path, "31,-88", "--run-date", text) == f"argument --run-date: expected YYYY-MM-DD, got {text!r}"


@pytest.mark.parametrize("text", ["temperature", "", ",", "temperature_2m,TMP"])
def test_bad_variables(capsys, tmp_path, text):
    message = error(capsys, tmp_path, "31,-88", "--variables", text)
    assert message == f"argument --variables: unknown or empty: {text!r}; choose from {', '.join(VARIABLES)}"


@pytest.mark.parametrize("text, hours", [("1", 1), ("48", 48), (" 2 ", 2)])
def test_num_hours_bounds(tmp_path, text, hours):
    assert parse(tmp_path, "31,-88", "--num-hours", text).num_hours == hours


@pytest.mark.parametrize("text", ["0", "49", "x", "1.5", ""])
def test_bad_num_hours(capsys, tmp_path, text):
    message = error(capsys, tmp_path, "31,-88", "--num-hours", text)
    assert message == f"argument --num-hours: expected a whole number from 1 to 48, got {text!r}"


def test_help_text(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    assert build_parser().format_help() == (
        "usage: hrrr-ingest [-h] [--run-date RUN_DATE] [--variables VARIABLES] [--num-hours NUM_HOURS] points\n"
        "\n"
        "Ingest NOAA HRRR 06z forecasts for a list of points into data.db (DuckDB).\n"
        "\n"
        "positional arguments:\n"
        "  points                text file with one 'latitude,longitude' per line\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --run-date RUN_DATE   06z run date, YYYY-MM-DD (default: the latest UTC date whose 06z run is complete)\n"
        "  --variables VARIABLES\n"
        "                        comma-separated variable names (default: all 11)\n"
        "  --num-hours NUM_HOURS\n"
        "                        forecast hours to ingest, f00 to f(N-1), 1-48 (default: 48)\n"
    )


def test_utc_today_is_the_utc_date_not_the_local_one(monkeypatch, utc_tz):
    from datetime import date, datetime, timezone

    from hrrr_ingest import cli

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2025, 4, 25, 2, 0, tzinfo=timezone.utc)  # 19:00 on 04-24 in Los Angeles
            return instant.astimezone(tz) if tz else instant.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Frozen)
    assert cli.utc_today() == date(2025, 4, 25)
