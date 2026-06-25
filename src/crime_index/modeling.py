from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.transform.index import build_index_dataframe


def build_modeled_baseline(
    year: int,
    comparison_scope: str | None = None,
    database_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    init_db(database_path)
    settings = settings or load_settings()
    baseline = settings.get("modeled_baseline", {})
    comparison_scope = comparison_scope or baseline.get("comparison_scope", "national_modeled_baseline")
    violent_rate = float(baseline.get("violent_rate_per_1000", 4.0))
    property_rate = float(baseline.get("property_rate_per_1000", 17.9))
    source_name = baseline.get("source_name", "national_modeled_baseline")
    notes = baseline.get(
        "notes",
        "Uniform national modeled baseline; not local incident data.",
    )

    with get_connection(database_path) as con:
        population = con.execute(
            """
            SELECT zcta, year, population_total
            FROM acs_zcta_population
            WHERE year = ?
              AND population_total IS NOT NULL
            ORDER BY zcta
            """,
            [year],
        ).fetchdf()
        if population.empty:
            return 0
        annual = build_modeled_annual_dataframe(
            population,
            violent_rate,
            property_rate,
            comparison_scope,
        )
        indexed = build_index_dataframe(annual, settings)
        indexed = _neutralize_modeled_scores(indexed, violent_rate, property_rate)
        indexed["coverage_status"] = "national_modeled"
        indexed["data_source_type"] = "modeled"
        indexed["source_names"] = source_name
        indexed["source_count"] = 1
        indexed["assigned_incident_count"] = 0
        indexed["spatial_incident_count"] = 0
        indexed["is_modeled"] = True
        indexed["data_coverage_score"] = indexed["population_total"].map(
            lambda population: 0.35 if pd.notna(population) and float(population) >= 500 else 0.20
        )
        indexed["confidence_grade"] = indexed["population_total"].map(
            lambda population: "C" if pd.notna(population) and float(population) >= 500 else "D"
        )
        indexed["score_notes"] = indexed["score_notes"].map(lambda value: _append_note(value, notes))
        con.execute(
            "DELETE FROM zcta_crime_index WHERE year = ? AND comparison_scope = ?",
            [year, comparison_scope],
        )
        _insert_df(con, "zcta_crime_index", indexed)
    return len(indexed)


def _neutralize_modeled_scores(
    indexed: pd.DataFrame,
    violent_rate_per_1000: float,
    property_rate_per_1000: float,
) -> pd.DataFrame:
    output = indexed.copy()
    valid_population = output["population_total"].notna() & (output["population_total"].astype(float) > 0)
    total_rate = violent_rate_per_1000 + property_rate_per_1000
    rate_values = {
        "violent_rate_per_1000": violent_rate_per_1000,
        "property_rate_per_1000": property_rate_per_1000,
        "total_rate_per_1000": total_rate,
        "violent_rate_winsorized_per_1000": violent_rate_per_1000,
        "property_rate_winsorized_per_1000": property_rate_per_1000,
        "total_rate_winsorized_per_1000": total_rate,
    }
    for column, value in rate_values.items():
        output.loc[valid_population, column] = value
        output.loc[~valid_population, column] = pd.NA

    neutral_score_columns = [
        "violent_score_0_100",
        "property_score_0_100",
        "total_crime_score_0_100",
        "overall_crime_score_0_100",
    ]
    neutral_percentile_columns = [
        "violent_percentile",
        "property_percentile",
        "total_crime_percentile",
        "overall_percentile",
        "percentile_rank",
    ]
    for column in neutral_score_columns:
        output.loc[valid_population, column] = 50.0
        output.loc[~valid_population, column] = pd.NA
    for column in neutral_percentile_columns:
        output.loc[valid_population, column] = 0.5
        output.loc[~valid_population, column] = pd.NA

    unavailable_score_columns = [
        "drug_score_0_100",
        "public_order_score_0_100",
        "weapons_score_0_100",
        "other_score_0_100",
        "drug_percentile",
        "public_order_percentile",
        "weapons_percentile",
        "other_percentile",
        "drug_rate_per_1000",
        "public_order_rate_per_1000",
        "weapons_rate_per_1000",
        "other_rate_per_1000",
        "unknown_rate_per_1000",
        "drug_rate_winsorized_per_1000",
        "public_order_rate_winsorized_per_1000",
        "weapons_rate_winsorized_per_1000",
        "other_rate_winsorized_per_1000",
    ]
    for column in unavailable_score_columns:
        if column in output:
            output[column] = pd.NA

    for column in [
        "violent_z_score",
        "property_z_score",
        "total_z_score",
        "drug_z_score",
        "public_order_z_score",
        "weapons_z_score",
        "other_z_score",
    ]:
        output.loc[valid_population, column] = 0.0
        output.loc[~valid_population, column] = pd.NA
    for column in ["violent_index", "property_index", "total_index", "composite_index"]:
        output.loc[valid_population, column] = 100.0
        output.loc[~valid_population, column] = pd.NA

    for column in [
        "violent_score_label",
        "property_score_label",
        "total_crime_score_label",
        "overall_crime_score_label",
    ]:
        output.loc[valid_population, column] = "average"
        output.loc[~valid_population, column] = "unavailable"
    for column in [
        "drug_score_label",
        "public_order_score_label",
        "weapons_score_label",
        "other_score_label",
    ]:
        output[column] = "unavailable"
    return output


def build_modeled_annual_dataframe(
    population: pd.DataFrame,
    violent_rate_per_1000: float,
    property_rate_per_1000: float,
    comparison_scope: str,
) -> pd.DataFrame:
    output = population.copy()
    output["comparison_scope"] = comparison_scope
    output["comparison_scope_value"] = ""
    valid_population = output["population_total"].notna() & (output["population_total"].astype(float) > 0)
    output["violent_crime_count"] = pd.NA
    output["property_crime_count"] = pd.NA
    output.loc[valid_population, "violent_crime_count"] = (
        output.loc[valid_population, "population_total"].astype(float) * violent_rate_per_1000 / 1000
    ).round().astype("int64")
    output.loc[valid_population, "property_crime_count"] = (
        output.loc[valid_population, "population_total"].astype(float) * property_rate_per_1000 / 1000
    ).round().astype("int64")
    output["total_crime_count"] = (
        output[["violent_crime_count", "property_crime_count"]].fillna(0).sum(axis=1).round().astype("int64")
    )
    for column in [
        "drug_crime_count",
        "public_order_crime_count",
        "weapons_crime_count",
        "other_crime_count",
        "unknown_crime_count",
    ]:
        output[column] = pd.NA
    return output


def _append_note(existing: object, note: str) -> str:
    if pd.isna(existing) or not str(existing).strip():
        return note
    text = str(existing)
    if note in text:
        return text
    return f"{text}; {note}"


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
