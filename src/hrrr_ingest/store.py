"""DuckDB storage: the hrrr_forecasts table, the missing-key query and the hourly insert.

Timestamps are naive UTC. A row's key is (run_time_utc, valid_time_utc, variable, latitude, longitude).
"""

import duckdb

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS hrrr_forecasts (
    valid_time_utc TIMESTAMP NOT NULL,
    run_time_utc   TIMESTAMP NOT NULL,
    latitude       FLOAT     NOT NULL,
    longitude      FLOAT     NOT NULL,
    variable       VARCHAR   NOT NULL,
    value          FLOAT     NOT NULL,
    source_s3      VARCHAR   NOT NULL,
    PRIMARY KEY (run_time_utc, valid_time_utc, variable, latitude, longitude)
)
"""

# The keys this run asks for, cast to the main table's types so float32 rounding matches exactly.
REQUESTED_SQL = """
CREATE OR REPLACE TEMP TABLE requested AS
SELECT
    unnest($valid)::TIMESTAMP AS valid_time_utc,
    $run::TIMESTAMP           AS run_time_utc,
    unnest($lat)::FLOAT       AS latitude,
    unnest($lon)::FLOAT       AS longitude,
    unnest($var)::VARCHAR     AS variable
"""

# Every (valid time, variable) block that has at least one requested key not yet stored.
MISSING_SQL = """
SELECT DISTINCT r.valid_time_utc, r.variable
FROM requested r
WHERE NOT EXISTS (
    SELECT 1 FROM hrrr_forecasts f
    WHERE f.run_time_utc = r.run_time_utc
      AND f.valid_time_utc = r.valid_time_utc
      AND f.variable = r.variable
      AND f.latitude = r.latitude
      AND f.longitude = r.longitude
)
ORDER BY r.valid_time_utc, r.variable
"""

# One statement per forecast hour, so an hour is stored completely or not at all.
# The casts are required: DuckDB 2.0 only detects the key conflict when the SELECT has the table's types.
INSERT_SQL = """
INSERT INTO hrrr_forecasts
SELECT
    unnest($valid)::TIMESTAMP,
    $run::TIMESTAMP,
    unnest($lat)::FLOAT,
    unnest($lon)::FLOAT,
    unnest($var)::VARCHAR,
    unnest($value)::FLOAT,
    unnest($source)::VARCHAR
ON CONFLICT DO NOTHING
"""


def connect(path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    con.execute(TABLE_DDL)
    return con


def missing(con, run_time, keys) -> list:
    """keys: (valid_time, variable, latitude, longitude) tuples. Returns the incomplete (valid_time, variable) blocks."""
    valid, var, lat, lon = (list(column) for column in zip(*keys))
    con.execute(REQUESTED_SQL, {"valid": valid, "run": run_time, "lat": lat, "lon": lon, "var": var})
    return con.execute(MISSING_SQL).fetchall()


def insert(con, run_time, rows) -> None:
    """rows: (valid_time, latitude, longitude, variable, value, source_s3) tuples for one forecast hour."""
    valid, lat, lon, var, value, source = (list(column) for column in zip(*rows))
    con.execute(
        INSERT_SQL,
        {"valid": valid, "run": run_time, "lat": lat, "lon": lon, "var": var, "value": value, "source": source},
    )
