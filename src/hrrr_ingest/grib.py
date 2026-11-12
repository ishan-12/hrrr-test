"""Decode one GRIB2 message with ecCodes, check it is the field that was asked for, find nearest points."""

from dataclasses import dataclass
from datetime import date

import eccodes
import numpy as np

from hrrr_ingest.inventory import Variable

EARTH_RADIUS_KM = 6371.0

# What a message says it is. typeOfFirstFixedSurface must be read as a number: as a string
# ecCodes returns "sfc" for both the ground surface (1) and a height above ground (103).
IDENTITY_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
    "level",
    "indicatorOfUnitOfTimeRange",
    "forecastTime",
    "dataDate",
    "dataTime",
)


class BadMessage(Exception):
    """The bytes are not a usable GRIB2 message for the requested field."""


@dataclass(frozen=True)
class Field:
    identity: tuple  # values of IDENTITY_KEYS
    grid_md5: str  # ecCodes md5Section3: a hash of the whole grid definition
    values: np.ndarray  # float64; NaN where the bitmap marks a value as missing


def expected_identity(variable: Variable, run_date: date, hour: int) -> tuple:
    hours = 1  # GRIB2 code table 4.4
    return (
        variable.discipline,
        variable.category,
        variable.number,
        variable.level_type,
        variable.level,
        hours,
        hour,
        int(f"{run_date:%Y%m%d}"),
        600,  # 06:00 UTC as HHMM
    )


def _open(data: bytes):
    try:
        return eccodes.codes_new_from_message(data)
    except eccodes.GribInternalError as e:
        raise BadMessage(f"ecCodes cannot read the message: {e}")


def decode(data: bytes) -> Field:
    handle = _open(data)
    try:
        identity = tuple(eccodes.codes_get(handle, key, ktype=int) for key in IDENTITY_KEYS)
        values = eccodes.codes_get_values(handle)
        if eccodes.codes_get(handle, "bitmapPresent"):
            values[values == eccodes.codes_get(handle, "missingValue")] = np.nan
        return Field(identity, eccodes.codes_get(handle, "md5Section3"), values)
    finally:
        eccodes.codes_release(handle)


def latlons(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of every grid point, in the same order as Field.values."""
    handle = _open(data)
    try:
        return eccodes.codes_get_array(handle, "latitudes"), eccodes.codes_get_array(handle, "longitudes")
    finally:
        eccodes.codes_release(handle)


def check(field: Field, expected: tuple, grid_md5: str | None) -> None:
    """Raise BadMessage unless the field is the expected one, on the expected grid (if one is given)."""
    if field.identity != expected:
        raise BadMessage(f"wrong field: expected {expected}, got {field.identity}")
    if grid_md5 is not None and field.grid_md5 != grid_md5:
        raise BadMessage(f"grid differs from this run's grid ({field.grid_md5} != {grid_md5})")


def values_at(field: Field, indices: list[int]) -> np.ndarray:
    """The field's values at the given grid points. A missing value is an error, never a zero."""
    values = field.values[indices]
    if not np.isfinite(values).all():
        raise BadMessage("missing or non-finite value at a requested grid point")
    return values


def _unit_vectors(lats, lons) -> np.ndarray:
    lat, lon = np.radians(lats), np.radians(lons)
    return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=-1)


def nearest(grid_lats, grid_lons, points) -> tuple[list[int], list[float]]:
    """For each (lat, lon) point: the index of the closest grid point and the great-circle distance in km.

    Works for longitudes in -180..180 or 0..360 alike. Ties go to the lowest index.
    """
    grid = _unit_vectors(grid_lats, grid_lons)
    indices, distances = [], []
    for point in _unit_vectors(*np.asarray(points, dtype=float).T):
        i = int(np.argmax(grid @ point))
        chord = float(np.linalg.norm(grid[i] - point))
        indices.append(i)
        distances.append(2 * EARTH_RADIUS_KM * np.arcsin(chord / 2))
    return indices, distances


def lon180(lon: float) -> float:
    return (lon + 180) % 360 - 180
