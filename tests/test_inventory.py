from pathlib import Path

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
