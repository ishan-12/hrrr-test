import dataclasses
from datetime import date
from pathlib import Path

import pytest

from hrrr_ingest import inventory
from hrrr_ingest.inventory import VARIABLES

FIXTURES = Path(__file__).parent / "fixtures"

SPEC_NAMES = [
    "surface_pressure",
    "surface_roughness",
    "visible_beam_downward_solar_flux",
    "visible_diffuse_downward_solar_flux",
    "temperature_2m",
    "dewpoint_2m",
    "relative_humidity_2m",
    "u_component_wind_10m",
    "v_component_wind_10m",
    "u_component_wind_80m",
    "v_component_wind_80m",
]


def test_registry_has_the_spec_names_in_spec_order():
    assert list(VARIABLES) == SPEC_NAMES
    assert all(name == v.name for name, v in VARIABLES.items())


def test_registry_entries_cannot_be_changed_at_run_time():
    with pytest.raises(dataclasses.FrozenInstanceError):
        VARIABLES["temperature_2m"].level = 10
    with pytest.raises(TypeError):
        VARIABLES["temperature_2m"] = VARIABLES["dewpoint_2m"]
    with pytest.raises(TypeError):
        del VARIABLES["temperature_2m"]


def test_registry_matches_identities_decoded_from_real_messages():
    # identities.txt is grib_get output for 44 real messages (11 fields x f00, f01, f02, f48).
    decoded = {}
    for line in (FIXTURES / "identities.txt").read_text().splitlines():
        if line.startswith("#"):
            continue
        hour, label, fields = line.split("|")
        discipline, category, number, level_type, level, step, _md5 = fields.split()
        assert int(step) == int(hour)
        identity = (int(discipline), int(category), int(number), int(level_type), int(level))
        assert decoded.setdefault(label, identity) == identity
    registry = {
        v.idx_label: (v.discipline, v.category, v.number, v.level_type, v.level)
        for v in VARIABLES.values()
    }
    assert registry == decoded


# --- T3: index planner, paths, run completeness -------------------------------------------------

IDX = (
    "1:0:d=2025042406:TMP:2 m above ground:anl:\n"
    "2:100:d=2025042406:UGRD:10 m above ground:anl:\n"
    "3:250:d=2025042406:UGRD:80 m above ground:anl:\n"
)


def test_byte_ranges_end_one_byte_before_the_next_message_or_at_end_of_file():
    assert inventory.byte_range(IDX, VARIABLES["temperature_2m"]) == (0, 99)
    assert inventory.byte_range(IDX, VARIABLES["u_component_wind_10m"]) == (100, 249)
    assert inventory.byte_range(IDX, VARIABLES["u_component_wind_80m"]) == (250, None)


@pytest.mark.parametrize(
    "idx, count",
    [(IDX, 0), (IDX + "4:400:d=2025042406:PRES:surface:anl:\n" * 2, 2)],
)
def test_missing_or_duplicate_label_is_an_error(idx, count):
    with pytest.raises(inventory.IdxError) as exc:
        inventory.byte_range(idx, VARIABLES["surface_pressure"])
    assert str(exc.value) == f"'PRES:surface' appears {count} times in the .idx file"


def test_real_idx_files_have_every_variable_exactly_once():
    for hour in ("00", "01", "02", "48"):
        text = (FIXTURES / f"hrrr.t06z.wrfsfcf{hour}.grib2.idx").read_text()
        for variable in VARIABLES.values():
            start, end = inventory.byte_range(text, variable)
            assert 0 <= start < end


def test_real_idx_byte_range_of_sfcr_matches_its_downloaded_size():
    text = (FIXTURES / "hrrr.t06z.wrfsfcf00.grib2.idx").read_text()
    start, end = inventory.byte_range(text, VARIABLES["surface_roughness"])
    assert end - start + 1 == (FIXTURES / "f00_sfcr_surface.grib2").stat().st_size


def test_paths_follow_the_bucket_layout_and_the_spec_source_format():
    d = date(2025, 1, 1)
    assert inventory.file_key(d, 0) == "hrrr.20250101/conus/hrrr.t06z.wrfsfcf00.grib2"
    assert inventory.http_url(d, 47) == (
        "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250101/conus/hrrr.t06z.wrfsfcf47.grib2"
    )
    assert inventory.source_s3(d, 0) == (
        "s3://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250101/conus/hrrr.t06z.wrfsfcf00.grib2"
    )


def test_a_run_is_complete_only_with_all_49_grib_and_idx_files():
    d = date(2025, 4, 24)
    keys = {inventory.file_key(d, h) + suffix for h in range(49) for suffix in ("", ".idx")}
    assert inventory.is_complete(keys, d)
    assert not inventory.is_complete(keys - {inventory.file_key(d, 48)}, d)
    assert not inventory.is_complete(keys - {inventory.file_key(d, 0) + ".idx"}, d)
    assert not inventory.is_complete((keys | {inventory.file_key(d, 49)}) - {inventory.file_key(d, 48) + ".idx"}, d)
    assert inventory.FIRST_COMPLETE_RUN == date(2020, 12, 3)


@pytest.mark.parametrize("idx", ["garbage\n", "1:0:d=2025042406:PRES:surface:anl:\n2:x:d=2025042406:TMP:2 m above ground:anl:\n"])
def test_a_malformed_idx_file_is_an_idx_error(idx):
    with pytest.raises(inventory.IdxError) as exc:
        inventory.byte_range(idx, VARIABLES["surface_pressure"])
    assert str(exc.value).startswith("malformed .idx file: ")
