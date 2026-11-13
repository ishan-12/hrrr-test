"""One ingest of one 06z run: find the missing rows, get only those messages (cache first), store them.

Order of work:
1. Read the grid from any valid cached message of the run, or download one message.
2. Snap each input point to its nearest grid cell; one key per cell.
3. Ask DuckDB which (hour, variable) blocks have any requested key missing.
4. For each such hour: get each block's message, check it, take the cell values, insert the hour.
"""

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb

from hrrr_ingest import grib, inventory, source, store

FAR_KM = 3.0  # inside HRRR's CONUS coverage every point is within about 2.1 km of a grid point
BLOCK_ERRORS = (source.FetchError, grib.BadMessage, inventory.IdxError)  # one (hour, variable) fails
STOP = "stopping the run (a rerun resumes from here)"


class HourUnavailable(Exception):
    """The hour's .idx could not be fetched, so none of its variables can be: give up on the hour once."""


def say(message: str) -> None:
    print(message, file=sys.stderr)


def latest_complete_date(today: date, get) -> date:
    """The newest UTC date, from today back to 2020-12-03, whose 06z run is fully published."""
    run_date = today
    while run_date >= inventory.FIRST_COMPLETE_RUN:
        if inventory.is_complete(source.list_keys(inventory.run_prefix(run_date), get), run_date):
            return run_date
        run_date -= timedelta(days=1)
    raise LookupError(f"no complete 06z run between {inventory.FIRST_COMPLETE_RUN} and {today}")


class Run:
    """Messages of one run: read from the cache when valid, otherwise byte-ranged from S3 and cached."""

    def __init__(self, run_date: date, cache_dir: Path, get):
        self.run_date = run_date
        self.cache_dir = cache_dir
        self.get = get
        self.idx = {}  # hour -> .idx text, fetched at most once per hour
        self.requests = 0
        self.bytes = 0

    def path(self, hour: int, variable) -> Path:
        return self.cache_dir / f"{self.run_date:%Y%m%d}" / f"f{hour:02d}" / f"{variable.name}.grib2"

    def fetch(self, url: str, start=None, end=None) -> bytes:
        self.requests += 1  # counted before the call, so failed requests count too
        data = self.get(url, start, end)
        self.bytes += len(data)
        return data

    def checked(self, data: bytes, hour: int, variable, grid_md5) -> grib.Field:
        if not source.is_complete_grib2(data):
            raise grib.BadMessage("not one complete GRIB2 message")
        field = grib.decode(data)
        grib.check(field, grib.expected_identity(variable, self.run_date, hour), grid_md5)
        return field

    def cached(self, hour: int, variable, grid_md5=None):
        """(bytes, field) from the cache, or None if nothing is cached. Raises BadMessage for a bad copy."""
        data = source.read_cached(self.path(hour, variable))
        return None if data is None else (data, self.checked(data, hour, variable, grid_md5))

    def download(self, hour: int, variable, grid_md5=None):
        url = inventory.http_url(self.run_date, hour)
        if hour not in self.idx:
            try:
                self.idx[hour] = self.fetch(url + ".idx").decode("ascii", "replace")
            except source.Unreachable:
                raise  # the bucket is down, not just this hour: the caller stops the run
            except source.FetchError as e:  # 404, 403: the whole hour is out of reach
                raise HourUnavailable(e)
        start, end = inventory.byte_range(self.idx[hour], variable)
        data = self.fetch(url, start, end)
        field = self.checked(data, hour, variable, grid_md5)
        try:
            source.write_atomic(self.path(hour, variable), data)
        except OSError as e:  # a full or read-only disk costs the cache, not the data
            say(f"f{hour:02d} {variable.name}: could not cache the message ({e.strerror}); using it anyway")
        return data, field

    def message(self, hour: int, variable, grid_md5):
        try:
            found = self.cached(hour, variable, grid_md5)
            if found:
                return found
        except grib.BadMessage as e:
            say(f"f{hour:02d} {variable.name}: cached copy rejected ({e}); downloading again")
        return self.download(hour, variable, grid_md5)

    def grid_message(self, hours, variables):
        """Any valid cached message of this run, else the first requested one that downloads."""
        for hour in range(inventory.LAST_HOUR + 1):
            for variable in inventory.VARIABLES.values():
                try:
                    found = self.cached(hour, variable)
                except grib.BadMessage:
                    continue  # reported later, if and when its own block is needed
                if found:
                    return found
        say(f"no usable cached message for {self.run_date}; downloading one to read the grid")
        for variable in variables:
            try:
                return self.download(hours[0], variable)
            except (HourUnavailable, source.Unreachable) as e:
                say(f"f{hours[0]:02d}: {e}")
                return None
            except BLOCK_ERRORS as e:
                say(f"f{hours[0]:02d} {variable.name}: {e}")
        return None


def ingest(con, run_date: date, hours, variables, points, cache_dir: Path, get) -> bool:
    """Store every requested row that is missing. True if all requested rows are stored afterwards."""
    run = Run(run_date, cache_dir, get)
    run_time = datetime.combine(run_date, time(6))
    found = run.grid_message(hours, variables)
    if found is None:
        say(f"{run_date} 06z: could not read the grid; nothing stored")
        return False
    data, grid_field = found
    lats, lons = grib.latlons(data)
    indices, distances = grib.nearest(lats, lons, points)
    for (lat, lon), km in zip(points, distances):
        if round(km, 1) > FAR_KM:  # judged on the distance as printed
            say(f"warning: point {lat},{lon} is {km:.1f} km from the nearest grid point")
    cells = list(dict.fromkeys(indices))
    coords = [(float(lats[i]), grib.lon180(float(lons[i]))) for i in cells]
    keys = [
        (run_time + timedelta(hours=hour), variable.name, lat, lon)
        for hour in hours
        for variable in variables
        for lat, lon in coords
    ]
    rows_before = con.execute("SELECT count(*) FROM hrrr_forecasts").fetchone()[0]

    todo = {}
    for valid_time, name in store.missing(con, run_time, keys):
        todo.setdefault(valid_time, []).append(inventory.VARIABLES[name])
    for valid_time, todo_variables in todo.items():
        hour = (valid_time - run_time) // timedelta(hours=1)
        source_s3 = inventory.source_s3(run_date, hour)
        rows, stop = [], False
        for variable in todo_variables:
            try:
                _, field = run.message(hour, variable, grid_field.grid_md5)
                values = grib.values_at(field, cells)
            except source.Unreachable as e:  # every later request would fail the same way
                say(f"f{hour:02d} {variable.name}: {e}; {STOP}")
                stop = True
                break
            except HourUnavailable as e:
                say(f"f{hour:02d}: {e}")
                break
            except BLOCK_ERRORS as e:
                say(f"f{hour:02d} {variable.name}: {e}")
                continue
            rows += [(valid_time, lat, lon, variable.name, float(v), source_s3) for (lat, lon), v in zip(coords, values)]
        if rows:  # rows gathered before a stop are stored too
            try:
                store.insert(con, run_time, rows)
            except duckdb.Error as e:  # a full disk or a broken database: every later insert would fail too
                say(f"f{hour:02d}: insert failed, nothing stored for this hour: {e}; {STOP}")
                stop = True
        if stop:
            break

    still_missing = store.missing(con, run_time, keys)
    rows_after = con.execute("SELECT count(*) FROM hrrr_forecasts").fetchone()[0]
    say(
        f"{run_date} 06z: {len(cells)} grid cells, {rows_after - rows_before} new rows, "
        f"{run.requests} requests ({run.bytes:,} bytes), "
        f"{len(still_missing)} (hour, variable) blocks still missing"
    )
    return not still_missing
