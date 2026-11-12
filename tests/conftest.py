"""Offline test scaffolding: tiny HRRR-like messages and a fake S3 bucket that counts every request.

Messages are real GRIB2: the committed f00 TMP message, shrunk to a 4 x 3 corner of the HRRR grid
(first point 21.138N, 122.72W), with the identity keys and values set per variable and hour.
"""

import functools
import time
import urllib.parse
from datetime import date
from pathlib import Path

import eccodes
import numpy as np
import pytest

from hrrr_ingest import inventory, source
from hrrr_ingest.inventory import VARIABLES

FIXTURES = Path(__file__).parent / "fixtures"
RUN_DATE = date(2025, 4, 24)

# Grid points of the 4 x 3 test grid, from ecCodes (longitude 0-360).
GRID_LATS = [21.138123, 21.14511004, 21.1520901, 21.1590632, 21.16299459, 21.1699845,
             21.17696744, 21.18394341, 21.18786863, 21.19486142, 21.20184723, 21.20882607]
GRID_LONS = [237.280472, 237.30713868, 237.3338097, 237.36048505, 237.27297501, 237.29964881,
             237.32632695, 237.35300943, 237.26547368, 237.2921546, 237.31883986, 237.34552947]


def point(cell: int) -> tuple[float, float]:
    """An input point a few metres from the given grid cell, with longitude in -180..180."""
    return round(GRID_LATS[cell], 4), round(GRID_LONS[cell] - 360, 4)


def expected_value(variable_name: str, hour: int, cell: int) -> float:
    return 100.0 * list(VARIABLES).index(variable_name) + 10.0 * hour + cell


@functools.cache
def _small_template() -> bytes:
    handle = eccodes.codes_new_from_message((FIXTURES / "f00_tmp_2m.grib2").read_bytes())
    eccodes.codes_set(handle, "Nx", 4)
    eccodes.codes_set(handle, "Ny", 3)
    eccodes.codes_set(handle, "packingType", "grid_simple")
    eccodes.codes_set_values(handle, np.zeros(12))
    message = eccodes.codes_get_message(handle)
    eccodes.codes_release(handle)
    return message


@functools.cache
def make_message(variable_name: str, run_date: date = RUN_DATE, hour: int = 0, **overrides) -> bytes:
    """A 12-point GRIB2 message that decodes as the given variable, run and hour."""
    variable = VARIABLES[variable_name]
    handle = eccodes.codes_new_from_message(_small_template())
    keys = {
        "discipline": variable.discipline,
        "parameterCategory": variable.category,
        "parameterNumber": variable.number,
        "typeOfFirstFixedSurface": variable.level_type,
        "scaleFactorOfFirstFixedSurface": 0,
        "scaledValueOfFirstFixedSurface": variable.level,
        "forecastTime": hour,
        "dataDate": int(f"{run_date:%Y%m%d}"),
    }
    for key, value in {**keys, **overrides}.items():
        eccodes.codes_set(handle, key, value)
    eccodes.codes_set_values(handle, [expected_value(variable_name, hour, cell) for cell in range(12)])
    message = eccodes.codes_get_message(handle)
    eccodes.codes_release(handle)
    return message


class FakeS3:
    """Stands in for source.fetch: serves objects, byte ranges and ListObjectsV2 pages, and logs calls."""

    def __init__(self):
        self.objects = {}  # https URL -> bytes
        self.calls = []  # (url, start, end)
        self.page_size = 1000
        self.failures = {}  # url -> exception to raise for every request to it

    def add_hour(self, run_date: date, hour: int, messages: dict[str, bytes]) -> None:
        """One GRIB2 file: the given {idx label: message} plus a trailing unrelated message, and its .idx."""
        body, lines = b"", []
        for n, (label, message) in enumerate({**messages, "HGT:surface": b"filler"}.items(), start=1):
            lines.append(f"{n}:{len(body)}:d={run_date:%Y%m%d}06:{label}:{hour} hour fcst:")
            body += message
        url = inventory.http_url(run_date, hour)
        self.objects[url] = body
        self.objects[url + ".idx"] = ("\n".join(lines) + "\n").encode()

    def add_run(self, run_date: date = RUN_DATE, hours=range(3), names=tuple(VARIABLES)) -> None:
        for hour in hours:
            self.add_hour(
                run_date,
                hour,
                {VARIABLES[name].idx_label: make_message(name, run_date, hour) for name in names},
            )

    def add_listing(self, run_date: date, hours=range(49)) -> None:
        """Keys only (no usable content), for default-date discovery."""
        for hour in hours:
            url = inventory.http_url(run_date, hour)
            self.objects.setdefault(url, b"")
            self.objects.setdefault(url + ".idx", b"")

    def __call__(self, url: str, start=None, end=None) -> bytes:
        self.calls.append((url, start, end))
        if url in self.failures:
            raise self.failures[url]
        if "?" in url:
            return self._listing(url)
        if url not in self.objects:
            raise source.NotPublished(f"not published: {url}")
        data = self.objects[url]
        return data if start is None else data[start : None if end is None else end + 1]

    def _listing(self, url: str) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query["list-type"] == ["2"]
        # A client that ignores the continuation token would page forever; fail it instead.
        assert self.calls.count((url, None, None)) == 1, f"listing page requested twice: {url}"
        offset = int(query.get("continuation-token", ["0"])[0])
        base = f"https://{inventory.BUCKET}/"
        keys = sorted(u.removeprefix(base) for u in self.objects if u.removeprefix(base).startswith(query["prefix"][0]))
        page = keys[offset : offset + self.page_size]
        more = offset + self.page_size < len(keys)
        xml = '<?xml version="1.0" encoding="UTF-8"?>'
        xml += '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        xml += "".join(f"<Contents><Key>{key}</Key></Contents>" for key in page)
        xml += f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
        if more:
            xml += f"<NextContinuationToken>{offset + self.page_size}</NextContinuationToken>"
        return (xml + "</ListBucketResult>").encode()

    def message_calls(self):
        """Byte-range requests (message downloads), in order."""
        return [call for call in self.calls if call[1] is not None]


@pytest.fixture
def s3(monkeypatch, tmp_path):
    """A fake bucket wired into source.fetch, with the working directory set to tmp_path."""
    fake = FakeS3()
    monkeypatch.setattr(source, "fetch", fake)
    monkeypatch.chdir(tmp_path)
    return fake


@pytest.fixture
def utc_tz(monkeypatch):
    """Run under a non-UTC local time zone, to prove nothing depends on it."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
