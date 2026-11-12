# HRRR variables, levels and bucket layout: what was verified

Checked on 2026-11-12 against the public bucket `noaa-hrrr-bdp-pds`, run 2025-04-24 06z, using only
`.idx` files, S3 listings and HTTP byte ranges (no whole GRIB2 file was downloaded). Messages were
decoded with `grib_ls`/`grib_get` from the `eccodeslib` 2.50 wheel (`.venv/.../eccodeslib/bin/`).

## The 11 variables

Each label matched **exactly one** line in the f00, f01, f02 and f48 `.idx` files (a check run with
`awk` over all four files). The decoded identity comes from `grib_get -p
discipline,parameterCategory,parameterNumber,typeOfFirstFixedSurface:i,level,step` on the
byte-ranged message. The full per-hour output (44 messages) is committed as
[`tests/fixtures/identities.txt`](../../tests/fixtures/identities.txt), and the test suite checks the
registry in `src/hrrr_ingest/inventory.py` against it.

| Stored name | `.idx` label | discipline/category/number | level type, value | ecCodes shortName | units |
| --- | --- | --- | --- | --- | --- |
| `surface_pressure` | `PRES:surface` | 0/3/0 | 1 (surface), 0 | `sp` | Pa |
| `surface_roughness` | `SFCR:surface` | **2**/0/1 | 1, 0 | `fsr` | m |
| `visible_beam_downward_solar_flux` | `VBDSF:surface` | 0/4/200 | 1, 0 | `vbdsf` | W m-2 |
| `visible_diffuse_downward_solar_flux` | `VDDSF:surface` | 0/4/201 | 1, 0 | `vddsf` | W m-2 |
| `temperature_2m` | `TMP:2 m above ground` | 0/0/0 | 103 (height above ground), 2 | `2t` | K |
| `dewpoint_2m` | `DPT:2 m above ground` | 0/0/6 | 103, 2 | `2d` | K |
| `relative_humidity_2m` | `RH:2 m above ground` | 0/1/1 | 103, 2 | `2r` | % |
| `u_component_wind_10m` | `UGRD:10 m above ground` | 0/2/2 | 103, 10 | `10u` | m s-1 |
| `v_component_wind_10m` | `VGRD:10 m above ground` | 0/2/3 | 103, 10 | `10v` | m s-1 |
| `u_component_wind_80m` | `UGRD:80 m above ground` | 0/2/2 | 103, 80 | `u` | m s-1 |
| `v_component_wind_80m` | `VGRD:80 m above ground` | 0/2/3 | 103, 80 | `v` | m s-1 |

This agrees row for row with the identity table the (private) build plan proposed; that table was
treated as a claim to check, not as a source.

Observations that shape the code:

- Surface roughness is in discipline 2 (land surface). Matching on category/number alone would
  confuse it with discipline-0 fields.
- `grib_ls` prints `typeOfFirstFixedSurface` as the abbreviation `sfc` for **every** field, including
  the 2 m and 10 m ones. The numeric value (`:i`) is 1 or 103. The code compares numbers.
- All fields are instantaneous (`productDefinitionTemplateNumber` 0); `step` equals the forecast hour
  (`stepUnits` 1 = hours) and `dataDate`/`dataTime` are `20250424`/`600`.
- `resolutionAndComponentFlags` is 8 on all messages: winds are **grid-relative**, not earth-relative.
- At f00 (06:00 UTC, night over CONUS) VBDSF and VDDSF are 188-byte `grid_simple` messages: a
  constant field of zeros. That is a real zero, not missing data. `bitmapPresent` is 0 on all 11.

Checked during review (2026-11-12 06z, f11, at 33.7845,-86.0524, from a byte-ranged
`DSWRF:surface` message and the stored values): DSWRF = 642.1, VBDSF = 905, VDDSF = 93.2 W m-2.
With the sun about 38° up, VBDSF × sin(38.1°) + VDDSF ≈ 651, close to DSWRF, while VBDSF + VDDSF
is far above it. So VBDSF behaves as beam irradiance on a surface facing the sun (direct normal),
not on a horizontal surface. One point and one hour; the values are stored as published either way.

## Grid

All 44 messages have the same `md5Section3` (`78367561440d7c7b608b8532a02e4780`): Lambert conformal,
1799 x 1059, `scanningMode` 64. ecCodes reports longitudes in 0-360.

Nearest grid point for the spec's 9 points (`grib_ls -l LAT,LON,1` on f00 TMP 2 m):

| Input | ecCodes index | Grid lat, lon (0-360) | Distance | TMP 2 m (K) |
| --- | --- | --- | --- | --- |
| 31.006900,-88.010300 | 479737 | 31.00, 272.00 | 1.31 km | 292.872 |
| 31.756900,-106.375000 | 525926 | 31.75, 253.63 | 0.48 km | 294.31 |
| 32.583889,-86.283060 | 596719 | 32.59, 273.70 | 1.43 km | 293.497 |
| 32.601700,-87.781100 | 587678 | 32.60, 272.23 | 1.15 km | 293.06 |
| 32.618900,-86.254800 | 598519 | 32.61, 273.74 | 0.96 km | 293.685 |
| 33.255300,-87.449500 | 632660 | 33.24, 272.54 | 1.83 km | 293.185 |
| 33.425878,-86.337550 | 652483 | 33.43, 273.66 | 0.68 km | 292.185 |
| 33.458665,-87.356820 | 647054 | 33.45, 272.63 | 1.54 km | 291.935 |
| 33.784500,-86.052400 | 677676 | 33.78, 273.94 | 0.42 km | 291.685 |

## Bucket layout and listings

- Object key: `hrrr.YYYYMMDD/conus/hrrr.t06z.wrfsfcfNN.grib2`, with `.idx` alongside.
- `.idx` line format: `n:byte_offset:d=YYYYMMDDHH:VAR:level:forecast:`. A message ends one byte
  before the next line's offset. The last message ends at the end of the file.
- A missing object returns HTTP 404 (`.../wrfsfcf49.grib2.idx`).
- ListObjectsV2 (`?list-type=2&prefix=hrrr.YYYYMMDD/conus/hrrr.t06z.wrfsfcf`) returned 98 keys
  (49 GRIB2 + 49 `.idx`) for 2025-04-24 and 2020-12-03, and 74 keys for 2020-12-02 (that run ended
  at f36). With `max-keys=3` the response carries `<IsTruncated>true</IsTruncated>` and a
  `<NextContinuationToken>`, in the `http://s3.amazonaws.com/doc/2006-03-01/` namespace.
