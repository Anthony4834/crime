from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.transform.rates import CATEGORIES, add_rate_columns
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)


COUNT_COLUMNS = ["total_crime_count"] + [f"{category}_crime_count" for category in CATEGORIES]
RATE_COLUMNS = ["total_rate_per_1000"] + [f"{category}_rate_per_1000" for category in CATEGORIES]


def aggregate_crime(
    year: int,
    database_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, int]:
    init_db(database_path)
    settings = settings or load_settings()
    rate_multiplier = settings.get("index", {}).get("rate_multiplier", 1000)
    include_population_only = bool(settings.get("aggregation", {}).get("include_population_only_zctas", False))

    with get_connection(database_path) as con:
        incidents = con.execute(
            """
            SELECT n.incident_id, n.incident_count, n.occurred_year, n.occurred_month, n.offense_group, a.zcta
            FROM normalized_crime_incidents n
            LEFT JOIN incident_zcta_assignment a USING (incident_id)
            WHERE n.occurred_year = ?
              AND a.zcta IS NOT NULL
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

        annual = build_annual_aggregates(incidents, population, year, rate_multiplier, include_population_only)
        monthly = build_monthly_aggregates(incidents, population, year, rate_multiplier, include_population_only)

        con.execute("DELETE FROM zcta_crime_annual WHERE year = ?", [year])
        con.execute("DELETE FROM zcta_crime_monthly WHERE year = ?", [year])
        _insert_df(con, "zcta_crime_annual", annual[_annual_columns()])
        _insert_df(con, "zcta_crime_monthly", monthly[_monthly_columns()])

    LOGGER.info("Built %s annual rows and %s monthly rows for %s", len(annual), len(monthly), year)
    return {"annual_rows": len(annual), "monthly_rows": len(monthly)}


def build_annual_aggregates(
    incidents: pd.DataFrame,
    population: pd.DataFrame,
    year: int,
    rate_multiplier: float = 1000.0,
    include_population_only_zctas: bool = False,
) -> pd.DataFrame:
    counts = _count_by_group(incidents, ["zcta", "occurred_year"]).rename(columns={"occurred_year": "year"})
    base_zctas = _base_zctas(population, counts, include_population_only_zctas)
    output = base_zctas.merge(counts, on=["zcta", "year"], how="left")
    output = _fill_count_columns(output)
    output = add_rate_columns(output, rate_multiplier)
    output["created_at"] = utc_now_naive()
    return output[_annual_columns()]


def build_monthly_aggregates(
    incidents: pd.DataFrame,
    population: pd.DataFrame,
    year: int,
    rate_multiplier: float = 1000.0,
    include_population_only_zctas: bool = False,
) -> pd.DataFrame:
    counts = _count_by_group(incidents, ["zcta", "occurred_year", "occurred_month"]).rename(
        columns={"occurred_year": "year", "occurred_month": "month"}
    )
    annual_base = _base_zctas(population, counts, include_population_only_zctas)
    months = pd.DataFrame({"month": list(range(1, 13))})
    base = annual_base.merge(months, how="cross")
    output = base.merge(counts, on=["zcta", "year", "month"], how="left")
    output = _fill_count_columns(output)
    output = add_rate_columns(output, rate_multiplier)
    output["created_at"] = utc_now_naive()
    return output[_monthly_columns()]


def _base_zctas(population: pd.DataFrame, counts: pd.DataFrame, include_population_only_zctas: bool) -> pd.DataFrame:
    if population.empty and counts.empty:
        return pd.DataFrame(columns=["zcta", "year", "population_total"])
    population_base = population[["zcta", "year", "population_total"]].drop_duplicates()
    count_base = counts[["zcta", "year"]].drop_duplicates() if not counts.empty else pd.DataFrame(columns=["zcta", "year"])
    if not include_population_only_zctas:
        return count_base.merge(population_base, on=["zcta", "year"], how="left")
    base = pd.concat(
        [
            population_base[["zcta", "year"]],
            count_base[["zcta", "year"]],
        ],
        ignore_index=True,
    ).drop_duplicates()
    return base.merge(population_base, on=["zcta", "year"], how="left")


def _count_by_group(incidents: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if incidents.empty:
        return pd.DataFrame(columns=group_cols + COUNT_COLUMNS)
    safe = incidents.copy()
    safe["offense_group"] = safe["offense_group"].fillna("unknown")
    count_values = safe["incident_count"] if "incident_count" in safe else pd.Series(1, index=safe.index)
    safe["incident_count"] = pd.to_numeric(count_values, errors="coerce").fillna(1).astype("int64")
    pivot = (
        safe.pivot_table(index=group_cols, columns="offense_group", values="incident_count", aggfunc="sum", fill_value=0)
        .reset_index()
        .rename_axis(None, axis=1)
    )
    for category in CATEGORIES:
        if category not in pivot.columns:
            pivot[category] = 0
    output = pivot[group_cols].copy()
    for category in CATEGORIES:
        output[f"{category}_crime_count"] = pivot[category].astype("int64")
    output["total_crime_count"] = output[[f"{category}_crime_count" for category in CATEGORIES]].sum(axis=1).astype("int64")
    return output[group_cols + COUNT_COLUMNS]


def _fill_count_columns(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    for column in COUNT_COLUMNS:
        if column not in output:
            output[column] = 0
        output[column] = output[column].fillna(0).astype("int64")
    return output


def _annual_columns() -> list[str]:
    return ["zcta", "year", "population_total"] + COUNT_COLUMNS + RATE_COLUMNS + ["created_at"]


def _monthly_columns() -> list[str]:
    return ["month", "zcta", "year", "population_total"] + COUNT_COLUMNS + RATE_COLUMNS + ["created_at"]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    con.execute(f"INSERT INTO {table} SELECT * FROM _insert_df")
    con.unregister("_insert_df")
