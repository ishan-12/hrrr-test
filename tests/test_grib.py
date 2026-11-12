"""T5 (real decoder and identity checks) and T6 (nearest point) on the committed HRRR fixtures."""

from datetime import date
from pathlib import Path

import eccodes
import numpy as np
import pytest

from conftest import make_message
from hrrr_ingest import grib
from hrrr_ingest.inventory import VARIABLES

FIXTURES = Path(__file__).parent / "fixtures"
HRRR_GRID_MD5 = "78367561440d7c7b608b8532a02e4780"
D = date(2025, 4, 24)
REAL = {
    "f00_tmp_2m.grib2": "temperature_2m",
    "f00_sfcr_surface.grib2": "surface_roughness",
    "f00_vbdsf_surface.grib2": "visible_beam_downward_solar_flux",
}
# The spec's 9 points, with the index, distance and TMP 2 m value grib_ls -l LAT,LON,1 reports
# (docs/research/hrrr-variables.md).
SPEC_POINTS = [
    (31.006900, -88.010300, 479737, 1.31, 292.872),
    (31.756900, -106.375000, 525926, 0.48, 294.31),
    (32.583889, -86.283060, 596719, 1.43, 293.497),
    (32.601700, -87.781100, 587678, 1.15, 293.06),
    (32.618900, -86.254800, 598519, 0.96, 293.685),
    (33.255300, -87.449500, 632660, 1.83, 293.185),
    (33.425878, -86.337550, 652483, 0.68, 292.185),
    (33.458665, -87.356820, 647054, 1.54, 291.935),
    (33.784500, -86.052400, 677676, 0.42, 291.685),
]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def hrrr_grid():
    return grib.latlons(fixture("f00_tmp_2m.grib2"))


def edited(data: bytes, **keys) -> bytes:
    handle = eccodes.codes_new_from_message(data)
    for key, value in keys.items():
        eccodes.codes_set(handle, key, value)
    message = eccodes.codes_get_message(handle)
    eccodes.codes_release(handle)
    return message


def test_expected_identity():
    assert grib.expected_identity(VARIABLES["u_component_wind_80m"], D, 7) == (0, 2, 2, 103, 80, 1, 7, 20250424, 600)


@pytest.mark.parametrize("name, variable", REAL.items())
def test_real_messages_decode_as_their_variable_on_the_hrrr_grid(name, variable):
    field = grib.decode(fixture(name))
    assert field.identity == grib.expected_identity(VARIABLES[variable], D, 0)
    assert field.grid_md5 == HRRR_GRID_MD5
    assert field.values.shape == (1799 * 1059,)
    grib.check(field, grib.expected_identity(VARIABLES[variable], D, 0), HRRR_GRID_MD5)


def test_surface_roughness_is_discipline_2_and_never_matches_a_discipline_0_field():
    field = grib.decode(fixture("f00_sfcr_surface.grib2"))
    assert field.identity[0] == 2
    for other in set(VARIABLES) - {"surface_roughness"}:
        with pytest.raises(grib.BadMessage):
            grib.check(field, grib.expected_identity(VARIABLES[other], D, 0), None)


@pytest.mark.parametrize("variable, run_date, hour", [("dewpoint_2m", D, 0), ("temperature_2m", D, 1), ("temperature_2m", date(2025, 4, 23), 0)])
def test_wrong_field_hour_or_date_is_rejected(variable, run_date, hour):
    field = grib.decode(fixture("f00_tmp_2m.grib2"))
    expected = grib.expected_identity(VARIABLES[variable], run_date, hour)
    with pytest.raises(grib.BadMessage) as exc:
        grib.check(field, expected, HRRR_GRID_MD5)
    assert str(exc.value) == f"wrong field: expected {expected}, got {field.identity}"


def test_tmp_values_match_grib_ls_at_the_spec_points():
    field = grib.decode(fixture("f00_tmp_2m.grib2"))
    indices = [index for *_, index, _km, _value in SPEC_POINTS]
    assert grib.values_at(field, indices) == pytest.approx([p[4] for p in SPEC_POINTS], abs=5e-3)


def test_night_time_solar_flux_is_a_real_zero():
    field = grib.decode(fixture("f00_vbdsf_surface.grib2"))
    assert len(fixture("f00_vbdsf_surface.grib2")) == 188
    assert (field.values == 0.0).all()
    assert list(grib.values_at(field, [0, 479737])) == [0.0, 0.0]


def test_a_changed_scanning_mode_is_caught_by_the_grid_check():
    field = grib.decode(edited(fixture("f00_vbdsf_surface.grib2"), scanningMode=0))
    expected = grib.expected_identity(VARIABLES["visible_beam_downward_solar_flux"], D, 0)
    grib.check(field, expected, None)  # same field, so only the grid check can catch it
    with pytest.raises(grib.BadMessage) as exc:
        grib.check(field, expected, HRRR_GRID_MD5)
    assert str(exc.value) == f"grid differs from this run's grid ({field.grid_md5} != {HRRR_GRID_MD5})"


def test_missing_value_under_a_bitmap_is_nan_and_rejects_the_block():
    handle = eccodes.codes_new_from_message(make_message("temperature_2m"))
    eccodes.codes_set(handle, "bitmapPresent", 1)
    values = np.arange(12.0)
    values[3] = eccodes.codes_get(handle, "missingValue")
    eccodes.codes_set_values(handle, values)
    field = grib.decode(eccodes.codes_get_message(handle))
    eccodes.codes_release(handle)
    assert np.isnan(field.values[3]) and np.isfinite(np.delete(field.values, 3)).all()
    assert list(grib.values_at(field, [2, 4])) == [2.0, 4.0]
    with pytest.raises(grib.BadMessage) as exc:
        grib.values_at(field, [2, 3])
    assert str(exc.value) == "missing or non-finite value at a requested grid point"


def test_values_without_a_bitmap_are_kept_even_if_they_equal_the_missing_value_marker():
    message = make_message("temperature_2m")
    handle = eccodes.codes_new_from_message(message)
    eccodes.codes_set_values(handle, np.full(12, eccodes.codes_get(handle, "missingValue")))
    field = grib.decode(eccodes.codes_get_message(handle))
    eccodes.codes_release(handle)
    assert (field.values == 9999.0).all()


@pytest.mark.parametrize("data", [b"", b"GRIB" + b"\0" * 20 + b"7777", b"not grib at all"])
def test_unreadable_bytes_are_a_bad_message(data):
    with pytest.raises(grib.BadMessage) as exc:
        grib.decode(data)
    assert str(exc.value).startswith("ecCodes cannot read the message: ")
    with pytest.raises(grib.BadMessage):
        grib.latlons(data)


def test_latlons_are_in_value_order(hrrr_grid):
    lats, lons = hrrr_grid
    assert lats.shape == lons.shape == (1799 * 1059,)
    assert (lats[0], lons[0]) == pytest.approx((21.138123, 237.280472))
    assert (lats[-1], lons[-1]) == pytest.approx((47.842195, 299.082807), abs=1e-5)


# --- T6: nearest point --------------------------------------------------------------------------


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def test_nearest_matches_eccodes_and_a_haversine_reference(hrrr_grid):
    lats, lons = hrrr_grid
    indices, distances = grib.nearest(lats, lons, [(p[0], p[1]) for p in SPEC_POINTS])
    assert indices == [p[2] for p in SPEC_POINTS]
    assert distances == pytest.approx([p[3] for p in SPEC_POINTS], abs=0.006)
    reference = [haversine_km(p[0], p[1], lats[i], lons[i]) for p, i in zip(SPEC_POINTS, indices)]
    assert distances == pytest.approx(reference, rel=1e-9)


def test_longitude_minus_88_is_the_same_as_272(hrrr_grid):
    west = grib.nearest(*hrrr_grid, [(31.0069, -88.0103)])
    east = grib.nearest(*hrrr_grid, [(31.0069, 271.9897)])
    assert west[0] == east[0] == [479737]
    assert west[1] == pytest.approx(east[1], abs=1e-6)


def test_ties_go_to_the_lowest_index():
    assert grib.nearest([0.0, 0.0, 50.0], [10.0, -10.0, 0.0], [(0.0, 0.0)])[0] == [0]
    assert grib.nearest([50.0, 0.0, 0.0], [0.0, -10.0, 10.0], [(0.0, 0.0)])[0] == [1]


def test_nearest_distance_on_a_known_offset():
    # One degree of latitude along a meridian is 6371 * pi / 180 km.
    indices, distances = grib.nearest([10.0, 30.0], [20.0, 20.0], [(11.0, 20.0)])
    assert indices == [0]
    assert distances[0] == pytest.approx(6371.0 * np.pi / 180, rel=1e-9)


@pytest.mark.parametrize(
    "lon, expected",
    [(272.0, -88.0), (-88.0, -88.0), (0.0, 0.0), (359.5, -0.5), (180.0, -180.0), (179.5, 179.5)],
)
def test_lon180(lon, expected):
    assert grib.lon180(lon) == pytest.approx(expected)
