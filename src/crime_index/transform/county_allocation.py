from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.transform.index import build_index_dataframe
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)

COUNTY_OBSERVED_SCOPE = "county_observed_allocated"
COUNTY_SOURCE_NAME = "fbi_cde_cius_offenses_known"


def build_county_crime_annual(
    year: int,
    database_path: str | Path | None = None,
) -> int:
    init_db(database_path)
    with get_connection(database_path) as con:
        offenses = con.execute(
            """
            SELECT *
            FROM fbi_cde_cius_agency_offenses
            WHERE year = ?
              AND county_fips IS NOT NULL
              AND county_fips != ''
            """,
            [year],
        ).fetchdf()
        population = con.execute(
            """
            SELECT zcta, year, population_total
            FROM acs_zcta_population
            WHERE year = ?
            """,
            [year],
        ).fetchdf()
        mapping = con.execute("SELECT * FROM zip_county_mapping").fetchdf()

        if offenses.empty or population.empty or mapping.empty:
            LOGGER.warning(
                "County annual build skipped: offenses=%s population=%s mapping=%s",
                len(offenses),
                len(population),
                len(mapping),
            )
            return 0

        county_population = build_county_population(mapping, population)
        county = build_county_annual_dataframe(offenses, county_population, year)
        con.execute("DELETE FROM county_crime_annual WHERE year = ?", [year])
        _insert_df(con, "county_crime_annual", county)
    LOGGER.info("Built %s county annual crime rows for %s", len(county), year)
    return len(county)


def build_county_observed_layer(
    year: int,
    comparison_scope: str = COUNTY_OBSERVED_SCOPE,
    database_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    init_db(database_path)
    settings = settings or load_settings()
    with get_connection(database_path) as con:
        county = con.execute(
            """
            SELECT *
            FROM county_crime_annual
            WHERE year = ?
              AND population_total IS NOT NULL
              AND population_total > 0
            """,
            [year],
        ).fetchdf()
        population = con.execute(
            """
            SELECT zcta, year, population_total
            FROM acs_zcta_population
            WHERE year = ?
            """,
            [year],
        ).fetchdf()
        mapping = con.execute("SELECT * FROM zip_county_mapping").fetchdf()

        if county.empty or population.empty or mapping.empty:
            LOGGER.warning(
                "County observed ZIP layer skipped: counties=%s population=%s mapping=%s",
                len(county),
                len(population),
                len(mapping),
            )
            return 0

        allocation = build_zcta_county_allocation_dataframe(mapping, population, county, year)
        annual = build_allocated_zcta_annual_dataframe(allocation, population, comparison_scope)
        indexed = build_index_dataframe(annual, settings)
        indexed = apply_county_observed_quality(indexed)

        con.execute("DELETE FROM zcta_county_crime_allocation WHERE year = ?", [year])
        _insert_df(con, "zcta_county_crime_allocation", allocation[_allocation_columns()])
        con.execute("DELETE FROM zcta_crime_index WHERE year = ? AND comparison_scope = ?", [year, comparison_scope])
        _insert_df(con, "zcta_crime_index", indexed[_table_columns(con, "zcta_crime_index", indexed)])
    LOGGER.info("Built %s county-observed allocated ZIP rows for %s", len(indexed), year)
    return len(indexed)


def build_county_population(mapping: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    pop = population[["zcta", "year", "population_total"]].copy()
    pop["population_total"] = pd.to_numeric(pop["population_total"], errors="coerce")
    joined = mapping.merge(pop, on="zcta", how="inner")
    joined["allocation_weight"] = pd.to_numeric(joined["allocation_weight"], errors="coerce").fillna(0.0)
    joined["allocated_population"] = joined["population_total"] * joined["allocation_weight"]
    county = (
        joined.groupby(["county_fips", "county_name", "state_code", "state_name", "year"], dropna=False)[
            "allocated_population"
        ]
        .sum()
        .reset_index()
    )
    county["population_total"] = county["allocated_population"].round().astype("Int64")
    return county.drop(columns=["allocated_population"])


def build_county_annual_dataframe(
    offenses: pd.DataFrame,
    county_population: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    safe = offenses.copy()
    for column in ["violent_crime_count", "property_crime_count"]:
        safe[column] = pd.to_numeric(safe[column], errors="coerce").fillna(0)
    grouped = safe.groupby(["county_fips", "county_name", "state_code", "state_name"], dropna=False).agg(
        violent_crime_count=("violent_crime_count", "sum"),
        property_crime_count=("property_crime_count", "sum"),
        agency_count=("agency_label", "count"),
        city_agency_count=("table_number", lambda values: int((values == "8").sum())),
        county_agency_count=("table_number", lambda values: int((values == "10").sum())),
        source_names=("table_name", lambda values: "|".join(sorted(set(str(value) for value in values)))),
    )
    county = grouped.reset_index()
    county["year"] = year
    county["total_crime_count"] = county["violent_crime_count"] + county["property_crime_count"]
    county = county.merge(
        county_population[["county_fips", "year", "population_total"]],
        on=["county_fips", "year"],
        how="left",
    )
    for count_column in ["violent_crime_count", "property_crime_count", "total_crime_count", "agency_count"]:
        county[count_column] = county[count_column].round().astype("Int64")
    county["violent_rate_per_1000"] = _rate(county["violent_crime_count"], county["population_total"])
    county["property_rate_per_1000"] = _rate(county["property_crime_count"], county["population_total"])
    county["total_rate_per_1000"] = _rate(county["total_crime_count"], county["population_total"])
    county["reporting_notes"] = (
        "FBI CDE CIUS 2024 agency tables; city and county agency rows are assigned to counties. "
        "Table 10 alone is not a county total, so this table combines county-tagged agency rows."
    )
    county["created_at"] = utc_now_naive()
    return county[_county_columns()].sort_values(["state_code", "county_fips"]).reset_index(drop=True)


def build_zcta_county_allocation_dataframe(
    mapping: pd.DataFrame,
    population: pd.DataFrame,
    county: pd.DataFrame,
    year: int,
) -> pd.DataFrame:
    pop = population[["zcta", "year", "population_total"]].copy()
    pop["population_total"] = pd.to_numeric(pop["population_total"], errors="coerce")
    mapping_base = mapping[["zcta", "county_fips", "allocation_weight"]].copy()
    county_rates = county[
        [
            "county_fips",
            "county_name",
            "state_code",
            "population_total",
            "total_rate_per_1000",
            "violent_rate_per_1000",
            "property_rate_per_1000",
            "source_names",
        ]
    ].rename(columns={"population_total": "county_population_total"})
    joined = mapping_base.merge(pop, on="zcta", how="inner").merge(county_rates, on="county_fips", how="inner")
    joined = joined[joined["population_total"].notna() & (joined["population_total"].astype(float) > 0)].copy()
    joined["allocation_weight"] = pd.to_numeric(joined["allocation_weight"], errors="coerce").fillna(0.0)
    joined["available_weight"] = joined.groupby("zcta")["allocation_weight"].transform("sum")
    joined = joined[joined["available_weight"] > 0].copy()
    joined["allocation_weight"] = joined["allocation_weight"] / joined["available_weight"]
    joined["zcta_population_total"] = joined["population_total"].round().astype("Int64")
    joined["allocated_total_crime_count"] = (
        joined["zcta_population_total"].astype(float) * joined["allocation_weight"] * joined["total_rate_per_1000"] / 1000
    )
    joined["allocated_violent_crime_count"] = (
        joined["zcta_population_total"].astype(float)
        * joined["allocation_weight"]
        * joined["violent_rate_per_1000"]
        / 1000
    )
    joined["allocated_property_crime_count"] = (
        joined["zcta_population_total"].astype(float)
        * joined["allocation_weight"]
        * joined["property_rate_per_1000"]
        / 1000
    )
    joined["county_total_rate_per_1000"] = joined["total_rate_per_1000"]
    joined["county_violent_rate_per_1000"] = joined["violent_rate_per_1000"]
    joined["county_property_rate_per_1000"] = joined["property_rate_per_1000"]
    joined["created_at"] = utc_now_naive()
    return joined[_allocation_columns()].sort_values(["zcta", "county_fips"]).reset_index(drop=True)


def build_allocated_zcta_annual_dataframe(
    allocation: pd.DataFrame,
    population: pd.DataFrame,
    comparison_scope: str,
) -> pd.DataFrame:
    grouped = allocation.groupby(["zcta", "year"], dropna=False).agg(
        total_crime_count=("allocated_total_crime_count", "sum"),
        violent_crime_count=("allocated_violent_crime_count", "sum"),
        property_crime_count=("allocated_property_crime_count", "sum"),
        county_count=("county_fips", "nunique"),
        county_fips=("county_fips", lambda values: "|".join(sorted(set(str(value) for value in values)))),
        county_name=("county_name", lambda values: "|".join(sorted(set(str(value) for value in values)))),
        county_components=(
            "county_fips",
            lambda values: json.dumps(
                [
                    {
                        "county_fips": str(row.county_fips),
                        "county_name": str(row.county_name),
                        "weight": round(float(row.allocation_weight), 6),
                    }
                    for row in allocation.loc[values.index].itertuples()
                ],
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    annual = grouped.reset_index()
    annual = annual.merge(population[["zcta", "year", "population_total"]], on=["zcta", "year"], how="left")
    for column in ["total_crime_count", "violent_crime_count", "property_crime_count"]:
        annual[column] = annual[column].round().astype("Int64")
    for column in [
        "drug_crime_count",
        "public_order_crime_count",
        "weapons_crime_count",
        "other_crime_count",
        "unknown_crime_count",
    ]:
        annual[column] = pd.NA
    annual["comparison_scope"] = comparison_scope
    annual["comparison_scope_value"] = ""
    annual["observed_level"] = "county"
    annual["allocation_method"] = "zip_county_rate_allocation"
    return annual


def apply_county_observed_quality(indexed: pd.DataFrame) -> pd.DataFrame:
    output = indexed.copy()
    output["coverage_status"] = "county_observed_allocated"
    output["data_source_type"] = "observed"
    output["source_names"] = COUNTY_SOURCE_NAME
    output["source_count"] = output["county_count"].fillna(1).astype("int64")
    output["assigned_incident_count"] = 0
    output["spatial_incident_count"] = 0
    output["is_modeled"] = False
    output["observed_level"] = "county"
    output["allocation_method"] = "zip_county_rate_allocation"
    output["data_coverage_score"] = output["population_total"].map(_county_coverage_score)
    output["confidence_grade"] = output.apply(_county_confidence_grade, axis=1)
    output["score_notes"] = output["score_notes"].map(
        lambda value: _append_note(
            value,
            "county_observed_allocated_from_fbi_cde_cius; ZIP values are estimated from county-level rates",
        )
    )
    return output


def _county_coverage_score(population: object) -> float:
    if pd.isna(population) or float(population) <= 0:
        return 0.0
    if float(population) < 500:
        return 0.55
    return 0.75


def _county_confidence_grade(row: pd.Series) -> str:
    population = row.get("population_total")
    if pd.isna(population) or float(population) <= 0:
        return "D"
    if float(population) < 500:
        return "C"
    return "B" if int(row.get("county_count") or 0) == 1 else "C"


def _append_note(existing: object, note: str) -> str:
    if pd.isna(existing) or not str(existing).strip():
        return note
    text = str(existing)
    if note in text:
        return text
    return f"{text}; {note}"


def _rate(count: pd.Series, population: pd.Series) -> pd.Series:
    count_numeric = pd.to_numeric(count, errors="coerce")
    pop_numeric = pd.to_numeric(population, errors="coerce")
    return (count_numeric / pop_numeric * 1000).where(pop_numeric > 0)


def _county_columns() -> list[str]:
    return [
        "county_fips",
        "county_name",
        "state_code",
        "state_name",
        "year",
        "population_total",
        "total_crime_count",
        "violent_crime_count",
        "property_crime_count",
        "total_rate_per_1000",
        "violent_rate_per_1000",
        "property_rate_per_1000",
        "agency_count",
        "city_agency_count",
        "county_agency_count",
        "source_names",
        "reporting_notes",
        "created_at",
    ]


def _allocation_columns() -> list[str]:
    return [
        "zcta",
        "year",
        "county_fips",
        "county_name",
        "state_code",
        "allocation_weight",
        "zcta_population_total",
        "county_population_total",
        "county_total_rate_per_1000",
        "county_violent_rate_per_1000",
        "county_property_rate_per_1000",
        "allocated_total_crime_count",
        "allocated_violent_crime_count",
        "allocated_property_crime_count",
        "source_names",
        "created_at",
    ]


def _table_columns(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> list[str]:
    table_columns = [row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    return [column for column in table_columns if column in df.columns]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
