from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.db import get_connection, init_db
from crime_index.utils.time_utils import utc_now_naive

COUNTY_OBSERVED_SCOPE = "county_observed_allocated"
COUNTY_SOURCE_NAME = "fbi_cde_cius_offenses_known"


def load_source_coverage(
    sources: dict[str, dict[str, Any]],
    database_path: str | Path | None = None,
) -> int:
    init_db(database_path)
    rows: list[dict[str, Any]] = []
    loaded_at = utc_now_naive()
    for source_name, source in sources.items():
        rows.append(
            {
                "source_name": source_name,
                "source_type": source.get("source_type", "local_incident"),
                "coverage_level": source.get("coverage_level", "jurisdiction"),
                "coverage_area_name": source.get("coverage_area_name") or source.get("jurisdiction_name"),
                "coverage_state": source.get("jurisdiction_state"),
                "source_url": source.get("source_url"),
                "source_year": source.get("source_year"),
                "data_start_date": source.get("data_start_date"),
                "data_end_date": source.get("data_end_date"),
                "update_cadence": source.get("update_cadence"),
                "has_point_coordinates": bool(source.get("latitude_column") and source.get("longitude_column")),
                "coordinate_quality": source.get("coordinate_quality", "unknown"),
                "offense_mapping_quality": source.get("offense_mapping_quality", "unknown"),
                "coverage_notes": source.get("coverage_notes"),
                "loaded_at": loaded_at,
            }
        )
    df = pd.DataFrame(rows)
    with get_connection(database_path) as con:
        con.execute("DELETE FROM source_coverage")
        if not df.empty:
            con.register("_source_coverage", df)
            con.execute("INSERT INTO source_coverage SELECT * FROM _source_coverage")
            con.unregister("_source_coverage")
    return len(df)


def build_national_coverage(
    year: int,
    database_path: str | Path | None = None,
) -> int:
    init_db(database_path)
    with get_connection(database_path) as con:
        population = con.execute(
            """
            SELECT zcta, year, population_total
            FROM acs_zcta_population
            WHERE year = ?
            """,
            [year],
        ).fetchdf()
        if population.empty:
            return 0
        incident_coverage = con.execute(
            """
            SELECT
                a.zcta,
                sum(coalesce(n.incident_count, 1)) AS assigned_incident_count,
                sum(CASE WHEN a.assignment_method = 'spatial_join' THEN coalesce(n.incident_count, 1) ELSE 0 END) AS spatial_incident_count,
                count(DISTINCT n.source_name) AS source_count,
                string_agg(DISTINCT n.source_name, '|') AS source_names
            FROM incident_zcta_assignment a
            JOIN normalized_crime_incidents n USING (incident_id)
            WHERE n.occurred_year = ?
              AND a.zcta IS NOT NULL
            GROUP BY a.zcta
            """,
            [year],
        ).fetchdf()
        output = population.merge(incident_coverage, on="zcta", how="left")
        county_coverage = con.execute(
            """
            SELECT
                zcta,
                coalesce(county_count, 1) AS county_source_count,
                coalesce(source_names, ?) AS county_source_names
            FROM zcta_crime_index
            WHERE year = ?
              AND comparison_scope = ?
              AND coverage_status = ?
            """,
            [COUNTY_SOURCE_NAME, year, COUNTY_OBSERVED_SCOPE, COUNTY_OBSERVED_SCOPE],
        ).fetchdf()
        if not county_coverage.empty:
            output = output.merge(county_coverage, on="zcta", how="left")
        else:
            output["county_source_count"] = pd.NA
            output["county_source_names"] = pd.NA

        output["source_count"] = output["source_count"].fillna(0).astype("int64")
        output["assigned_incident_count"] = output["assigned_incident_count"].fillna(0).astype("int64")
        output["spatial_incident_count"] = output["spatial_incident_count"].fillna(0).astype("int64")
        output["source_names"] = output["source_names"].fillna("")
        direct_observed = output["assigned_incident_count"].astype("int64") > 0
        county_observed = output["county_source_count"].notna() & ~direct_observed
        output.loc[county_observed, "source_count"] = (
            pd.to_numeric(output.loc[county_observed, "county_source_count"], errors="coerce").fillna(1).astype("int64")
        )
        output.loc[county_observed, "source_names"] = output.loc[county_observed, "county_source_names"].fillna(
            COUNTY_SOURCE_NAME
        )
        output["coverage_status"] = "national_modeled"
        output.loc[direct_observed, "coverage_status"] = "observed"
        output.loc[county_observed, "coverage_status"] = COUNTY_OBSERVED_SCOPE
        output["data_source_type"] = output["coverage_status"].map(
            lambda status: "modeled" if status == "national_modeled" else "observed"
        )
        output["coverage_notes"] = "No local incident source or county-observed source loaded for this ZCTA/year."
        output.loc[direct_observed, "coverage_notes"] = None
        output.loc[county_observed, "coverage_notes"] = (
            "FBI CDE CIUS county-observed data allocated to ZCTA using Census ZCTA-county relationship weights."
        )
        output["created_at"] = utc_now_naive()
        con.execute("DELETE FROM zcta_national_coverage WHERE year = ?", [year])
        con.register("_national_coverage", output[_coverage_columns()])
        con.execute("INSERT INTO zcta_national_coverage SELECT * FROM _national_coverage")
        con.unregister("_national_coverage")
    return len(output)


def export_national_coverage(
    year: int,
    output_dir: str | Path = "data/exports",
    database_path: str | Path | None = None,
) -> dict[str, str]:
    init_db(database_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with get_connection(database_path) as con:
        df = con.execute(
            """
            SELECT *
            FROM zcta_national_coverage
            WHERE year = ?
            ORDER BY zcta
            """,
            [year],
        ).fetchdf()
    if df.empty:
        return {}
    csv_path = output_dir / f"zcta_national_coverage_{year}.csv"
    parquet_path = output_dir / f"zcta_national_coverage_{year}.parquet"
    df.to_csv(csv_path, index=False)
    df.to_parquet(parquet_path, index=False)
    return {"csv": str(csv_path), "parquet": str(parquet_path)}


def _coverage_columns() -> list[str]:
    return [
        "zcta",
        "year",
        "population_total",
        "source_count",
        "source_names",
        "assigned_incident_count",
        "spatial_incident_count",
        "coverage_status",
        "data_source_type",
        "coverage_notes",
        "created_at",
    ]
