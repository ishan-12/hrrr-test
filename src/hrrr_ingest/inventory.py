"""What lives where in the HRRR bucket: the 11 supported variables, S3 paths and .idx byte ranges."""

from dataclasses import dataclass


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
