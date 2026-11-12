"""What lives where in the HRRR bucket: the 11 supported variables, S3 paths and .idx byte ranges."""

from dataclasses import dataclass
from datetime import date

BUCKET = "noaa-hrrr-bdp-pds.s3.amazonaws.com"
FIRST_COMPLETE_RUN = date(2020, 12, 3)  # the first 06z run published with all 49 hours
LAST_HOUR = 48  # a complete 06z run has f00 to f48


@dataclass(frozen=True)
class Variable:
    name: str  # the name stored in hrrr_forecasts.variable
    idx_label: str  # "VAR:level" exactly as written in the .idx file
    discipline: int
    category: int
    number: int
    level_type: int  # GRIB2 code table 4.5: 1 = ground surface, 103 = height above ground (m)
    level: int


# Verified with grib_get on 2025-04-24 06z f00/f01/f02/f48 messages; see docs/research/hrrr-variables.md.
VARIABLES = {
    v.name: v
    for v in (
        Variable("surface_pressure", "PRES:surface", 0, 3, 0, 1, 0),
        Variable("surface_roughness", "SFCR:surface", 2, 0, 1, 1, 0),
        Variable("visible_beam_downward_solar_flux", "VBDSF:surface", 0, 4, 200, 1, 0),
        Variable("visible_diffuse_downward_solar_flux", "VDDSF:surface", 0, 4, 201, 1, 0),
        Variable("temperature_2m", "TMP:2 m above ground", 0, 0, 0, 103, 2),
        Variable("dewpoint_2m", "DPT:2 m above ground", 0, 0, 6, 103, 2),
        Variable("relative_humidity_2m", "RH:2 m above ground", 0, 1, 1, 103, 2),
        Variable("u_component_wind_10m", "UGRD:10 m above ground", 0, 2, 2, 103, 10),
        Variable("v_component_wind_10m", "VGRD:10 m above ground", 0, 2, 3, 103, 10),
        Variable("u_component_wind_80m", "UGRD:80 m above ground", 0, 2, 2, 103, 80),
        Variable("v_component_wind_80m", "VGRD:80 m above ground", 0, 2, 3, 103, 80),
    )
}


def run_prefix(run_date: date) -> str:
    return f"hrrr.{run_date:%Y%m%d}/conus/hrrr.t06z.wrfsfcf"


def file_key(run_date: date, hour: int) -> str:
    return f"{run_prefix(run_date)}{hour:02d}.grib2"


def http_url(run_date: date, hour: int) -> str:
    return f"https://{BUCKET}/{file_key(run_date, hour)}"


def source_s3(run_date: date, hour: int) -> str:
    # The spec's format: an s3:// URL that names the bucket's https host.
    return f"s3://{BUCKET}/{file_key(run_date, hour)}"


def is_complete(keys: set[str], run_date: date) -> bool:
    """True if the listing has every GRIB2 file f00-f48 of the run and the .idx for each."""
    files = [file_key(run_date, hour) for hour in range(LAST_HOUR + 1)]
    return all(f in keys and f + ".idx" in keys for f in files)


class IdxError(Exception):
    """The .idx file does not locate exactly one message for the variable."""


def byte_range(idx_text: str, variable: Variable) -> tuple[int, int | None]:
    """First and last byte of the variable's message; last is None for the file's final message.

    Raises IdxError unless the label appears exactly once in a well-formed .idx file.
    """
    try:
        entries = [line.split(":") for line in idx_text.splitlines()]
        matches = [i for i, e in enumerate(entries) if f"{e[3]}:{e[4]}" == variable.idx_label]
        if len(matches) != 1:
            raise IdxError(f"{variable.idx_label!r} appears {len(matches)} times in the .idx file")
        i = matches[0]
        end = int(entries[i + 1][1]) - 1 if i + 1 < len(entries) else None
        return int(entries[i][1]), end
    except (IndexError, ValueError) as e:
        raise IdxError(f"malformed .idx file: {e}")
