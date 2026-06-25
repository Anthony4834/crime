from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.transform.rates import add_rate_columns
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)

SCORE_CATEGORIES = ["violent", "property", "drug", "public_order", "weapons", "other"]
RATE_BASES = ["total"] + SCORE_CATEGORIES
RATE_COLUMNS = [f"{base}_rate_per_1000" if base != "total" else "total_rate_per_1000" for base in RATE_BASES]
WINSORIZED_COLUMNS = [
    f"{base}_rate_winsorized_per_1000" if base != "total" else "total_rate_winsorized_per_1000"
    for base in RATE_BASES
]
SCORE_COLUMNS = ["total_crime_score_0_100"] + [f"{category}_score_0_100" for category in SCORE_CATEGORIES]
PERCENTILE_COLUMNS = ["total_crime_percentile"] + [f"{category}_percentile" for category in SCORE_CATEGORIES]
Z_SCORE_COLUMNS = [f"{category}_z_score" for category in SCORE_CATEGORIES] + ["total_z_score"]
COUNT_COLUMNS = ["total_crime_count"] + [f"{category}_crime_count" for category in SCORE_CATEGORIES] + ["unknown_crime_count"]


def build_index(
    year: int,
    comparison_scope: str = "source_universe",
    database_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    init_db(database_path)
    settings = settings or load_settings()
    with get_connection(database_path) as con:
        annual = con.execute("SELECT * FROM zcta_crime_annual WHERE year = ?", [year]).fetchdf()
        if annual.empty:
            LOGGER.warning("No annual crime rows found for %s", year)
            return 0
        scoped = apply_comparison_scope(con, annual, comparison_scope)
        indexed = build_index_dataframe(scoped, settings, con, year)
        con.execute(
            "DELETE FROM zcta_crime_index WHERE year = ? AND comparison_scope = ?",
            [year, comparison_scope],
        )
        _insert_df(con, "zcta_crime_index", indexed[_index_columns()])
    LOGGER.info("Built %s crime score rows for %s / %s", len(indexed), year, comparison_scope)
    return len(indexed)


def calculate_category_rates(df: pd.DataFrame, population_col: str = "population_total") -> pd.DataFrame:
    rename_needed = population_col != "population_total"
    working = df.rename(columns={population_col: "population_total"}) if rename_needed else df.copy()
    rated = add_rate_columns(working)
    return rated.rename(columns={"population_total": population_col}) if rename_needed else rated


def winsorize_rates(
    df: pd.DataFrame,
    rate_columns: list[str],
    lower_pct: float,
    upper_pct: float,
    group_cols: list[str],
) -> pd.DataFrame:
    output = df.copy()
    for rate_column in rate_columns:
        winsorized_column = _winsorized_column(rate_column)
        output[winsorized_column] = output.groupby(group_cols, dropna=False)[rate_column].transform(
            lambda series: _clip_series(series, lower_pct, upper_pct)
        )
    return output


def calculate_percentile_scores(
    df: pd.DataFrame,
    rate_columns: list[str],
    group_cols: list[str],
    ranking_method: str = "average",
    round_digits: int = 1,
) -> pd.DataFrame:
    output = df.copy()
    for rate_column in rate_columns:
        base = _rate_base(rate_column)
        score_column = "total_crime_score_0_100" if base == "total" else f"{base}_score_0_100"
        percentile_column = "total_crime_percentile" if base == "total" else f"{base}_percentile"
        percentile = output.groupby(group_cols, dropna=False)[rate_column].transform(
            lambda series: _percent_rank(series, ranking_method)
        )
        output[percentile_column] = percentile
        output[score_column] = (percentile * 100).round(round_digits)
    return output


def calculate_z_scores(df: pd.DataFrame, rate_columns: list[str], group_cols: list[str]) -> pd.DataFrame:
    output = df.copy()
    for rate_column in rate_columns:
        base = _rate_base(rate_column)
        z_column = f"{base}_z_score"
        output[z_column] = output.groupby(group_cols, dropna=False)[rate_column].transform(_z_score)
    return output


def calculate_logistic_scores(
    df: pd.DataFrame,
    z_score_columns: list[str],
    round_digits: int = 1,
) -> pd.DataFrame:
    output = df.copy()
    for z_score_column in z_score_columns:
        score_column = z_score_column.replace("_z_score", "_logistic_score_0_100")
        output[score_column] = output[z_score_column].map(
            lambda value: None if pd.isna(value) else round(100 / (1 + math.exp(-float(value))), round_digits)
        )
    return output


def calculate_overall_score(
    df: pd.DataFrame,
    category_score_columns: list[str],
    weights: dict[str, float],
    round_digits: int = 1,
    renormalize_missing: bool = True,
) -> pd.DataFrame:
    output = df.copy()
    scores: list[float | None] = []
    notes: list[str | None] = []
    for _, row in output.iterrows():
        weighted_values: list[tuple[float, float, str]] = []
        missing: list[str] = []
        for column in category_score_columns:
            category = column.replace("_score_0_100", "")
            value = row.get(column)
            weight = float(weights.get(category, 0.0))
            if weight <= 0:
                continue
            if pd.isna(value):
                missing.append(category)
            else:
                weighted_values.append((float(value), weight, category))
        available_weight = sum(weight for _, weight, _ in weighted_values)
        if available_weight <= 0:
            scores.append(None)
            notes.append("overall_score_unavailable_no_category_scores")
            continue
        denominator = available_weight if renormalize_missing else sum(weights.values())
        scores.append(round(sum(value * weight for value, weight, _ in weighted_values) / denominator, round_digits))
        if missing:
            notes.append("renormalized_missing_categories:" + ",".join(missing))
        else:
            notes.append(None)
    output["overall_crime_score_0_100"] = scores
    output["score_notes"] = notes
    return output


def assign_score_labels(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    label_map = {
        "violent_score_0_100": "violent_score_label",
        "property_score_0_100": "property_score_label",
        "drug_score_0_100": "drug_score_label",
        "public_order_score_0_100": "public_order_score_label",
        "weapons_score_0_100": "weapons_score_label",
        "other_score_0_100": "other_score_label",
        "total_crime_score_0_100": "total_crime_score_label",
        "overall_crime_score_0_100": "overall_crime_score_label",
    }
    for score_column, label_column in label_map.items():
        output[label_column] = output[score_column].map(score_label)
    return output


def calculate_confidence_grade(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    output["confidence_grade"] = output.apply(_confidence_grade, axis=1)
    return output


def build_index_dataframe(
    annual: pd.DataFrame,
    settings: dict[str, Any],
    con: duckdb.DuckDBPyConnection | None = None,
    year: int | None = None,
) -> pd.DataFrame:
    output = annual.copy()
    group_cols = ["year", "comparison_scope", "comparison_scope_value"]
    score_settings = settings.get("score_model", {})
    winsor_settings = score_settings.get("winsorization", {})
    lower = winsor_settings.get("lower_pct", settings.get("index", {}).get("winsorize_lower_pct", 0.01))
    upper = winsor_settings.get("upper_pct", settings.get("index", {}).get("winsorize_upper_pct", 0.99))
    percentile_settings = score_settings.get("percentile", {})
    round_digits = int(percentile_settings.get("round_digits", 1))
    ranking_method = percentile_settings.get("ranking_method", "average")

    output = calculate_category_rates(output)
    output = winsorize_rates(output, RATE_COLUMNS, lower, upper, group_cols)
    output = calculate_percentile_scores(output, WINSORIZED_COLUMNS, group_cols, ranking_method, round_digits)
    output = calculate_z_scores(output, WINSORIZED_COLUMNS, group_cols)
    output = _calculate_internal_indexes(output, settings)
    output = calculate_overall_score(
        output,
        [f"{category}_score_0_100" for category in SCORE_CATEGORIES],
        score_settings.get("overall_weights", {}),
        round_digits,
        bool(score_settings.get("renormalize_weights_when_missing", True)),
    )
    output["overall_percentile"] = output.groupby(group_cols, dropna=False)["overall_crime_score_0_100"].transform(
        lambda series: _percent_rank(series, ranking_method)
    )
    output["percentile_rank"] = output["overall_percentile"]
    output = assign_score_labels(output)

    coverage = calculate_coverage_metrics(con, year) if con is not None and year is not None else {}
    output = _apply_quality_metrics(output, coverage, settings)
    output = calculate_confidence_grade(output)
    output["created_at"] = utc_now_naive()
    output["comparison_scope_value"] = output["comparison_scope_value"].fillna("")
    output = _ensure_index_columns(output)
    return output[_index_columns()]


def apply_comparison_scope(
    con: duckdb.DuckDBPyConnection,
    annual: pd.DataFrame,
    comparison_scope: str,
) -> pd.DataFrame:
    output = annual.copy()
    output["comparison_scope"] = comparison_scope
    if comparison_scope == "state":
        states = con.execute("SELECT zcta, state_fips FROM zcta_geometries").fetchdf().drop_duplicates("zcta")
        output = output.merge(states, on="zcta", how="left")
        output["comparison_scope_value"] = output["state_fips"].fillna("unknown")
        output = output.drop(columns=["state_fips"])
    else:
        output["comparison_scope_value"] = ""
    return output


def calculate_coverage_metrics(
    con: duckdb.DuckDBPyConnection | None,
    year: int | None,
) -> dict[str, Any]:
    if con is None or year is None:
        return {}
    normalized = con.execute(
        """
        SELECT incident_id, occurred_date, offense_group, coalesce(incident_count, 1) AS incident_count
        FROM normalized_crime_incidents
        WHERE occurred_year = ?
        """,
        [year],
    ).fetchdf()
    assignments = con.execute(
        """
        SELECT a.incident_id, a.zcta, a.assignment_method,
               n.source_name,
               coalesce(n.incident_count, 1) AS incident_count
        FROM incident_zcta_assignment a
        JOIN normalized_crime_incidents n USING (incident_id)
        WHERE n.occurred_year = ?
        """,
        [year],
    ).fetchdf()
    total = int(normalized["incident_count"].sum()) if not normalized.empty else 0
    assigned = int(assignments.loc[assignments["zcta"].notna(), "incident_count"].sum()) if not assignments.empty else 0
    spatial = (
        int(assignments.loc[assignments["assignment_method"] == "spatial_join", "incident_count"].sum())
        if not assignments.empty
        else 0
    )
    offense_known = (
        int(normalized.loc[normalized["offense_group"] != "unknown", "incident_count"].sum()) if not normalized.empty else 0
    )
    date_complete = (
        int(normalized.loc[normalized["occurred_date"].notna(), "incident_count"].sum()) if not normalized.empty else 0
    )
    by_zcta = pd.DataFrame(columns=["zcta", "assigned_count", "spatial_count", "spatial_assignment_share"])
    if not assignments.empty:
        assigned_rows = assignments.dropna(subset=["zcta"]).copy()
        if not assigned_rows.empty:
            assigned_rows["spatial_incident_count"] = assigned_rows["incident_count"].where(
                assigned_rows["assignment_method"] == "spatial_join",
                0,
            )
            by_zcta = assigned_rows.groupby("zcta").agg(
                assigned_count=("incident_count", "sum"),
                spatial_count=("spatial_incident_count", "sum"),
                source_count=("source_name", "nunique"),
                source_names=("source_name", lambda values: "|".join(sorted(set(str(value) for value in values)))),
            ).reset_index()
            by_zcta["spatial_assignment_share"] = by_zcta["spatial_count"] / by_zcta["assigned_count"]
    return {
        "zcta_assignment_rate": float(assigned / total) if total else 0.0,
        "spatial_assignment_rate": float(spatial / total) if total else 0.0,
        "offense_classification_rate": float(offense_known / total) if total else 0.0,
        "date_completeness_rate": float(date_complete / total) if total else 0.0,
        "by_zcta": by_zcta,
    }


def score_label(score: object) -> str:
    if pd.isna(score):
        return "unavailable"
    value = float(score)
    if value < 20:
        return "very_low"
    if value < 40:
        return "low"
    if value < 60:
        return "average"
    if value < 80:
        return "high"
    return "very_high"


def _apply_quality_metrics(df: pd.DataFrame, coverage: dict[str, Any], settings: dict[str, Any]) -> pd.DataFrame:
    output = df.copy()
    by_zcta = coverage.get("by_zcta")
    if isinstance(by_zcta, pd.DataFrame) and not by_zcta.empty:
        output = output.merge(by_zcta, on="zcta", how="left")
    else:
        output["assigned_count"] = pd.NA
        output["spatial_count"] = pd.NA
        output["source_count"] = pd.NA
        output["source_names"] = pd.NA
        output["spatial_assignment_share"] = pd.NA

    output["assigned_incident_count"] = output["assigned_count"].fillna(0).astype("int64")
    output["spatial_incident_count"] = output["spatial_count"].fillna(0).astype("int64")
    output["source_count"] = output["source_count"].fillna(0).astype("int64")
    output["source_names"] = output["source_names"].fillna("")
    output["coverage_status"] = output["assigned_incident_count"].map(
        lambda count: "observed" if int(count) > 0 else "national_modeled"
    )
    output["data_source_type"] = output["assigned_incident_count"].map(
        lambda count: "observed" if int(count) > 0 else "modeled"
    )
    output["is_modeled"] = False
    output["spatial_assignment_share"] = output["spatial_assignment_share"].fillna(
        coverage.get("spatial_assignment_rate", 0.0)
    )
    population_available = output["population_total"].notna().astype(float)
    output["data_coverage_score"] = (
        0.35 * coverage.get("zcta_assignment_rate", 0.0)
        + 0.25 * output["spatial_assignment_share"]
        + 0.20 * coverage.get("offense_classification_rate", 0.0)
        + 0.10 * coverage.get("date_completeness_rate", 0.0)
        + 0.10 * population_available
    ).round(3)

    low_count_threshold = settings.get("index", {}).get("low_count_threshold", 5)
    low_count_note = output["total_crime_count"].fillna(0) < low_count_threshold
    output["score_notes"] = output.apply(
        lambda row: _append_note(row.get("score_notes"), "low_incident_count")
        if row.name in set(output.index[low_count_note])
        else row.get("score_notes"),
        axis=1,
    )
    output = output.drop(columns=["assigned_count", "spatial_count"], errors="ignore")
    return output


def _append_note(existing: str | None, note: str) -> str:
    if existing is None or pd.isna(existing) or not str(existing).strip():
        return note
    if note in str(existing).split("; "):
        return str(existing)
    return f"{existing}; {note}"


def _confidence_grade(row: pd.Series) -> str:
    population = row.get("population_total")
    total = row.get("total_crime_count") or 0
    spatial_share = row.get("spatial_assignment_share") or 0
    coverage = row.get("data_coverage_score") or 0
    if pd.isna(population) or float(population) <= 0:
        return "D"
    if float(population) < 100:
        return "D"
    if spatial_share >= 0.90 and coverage >= 0.85 and total >= 20 and float(population) >= 1000:
        return "A"
    if total < 5:
        return "C"
    if spatial_share >= 0.70 and coverage >= 0.70 and float(population) >= 500:
        return "B"
    if coverage >= 0.50 and float(population) >= 100:
        return "C"
    return "D"


def _calculate_internal_indexes(df: pd.DataFrame, settings: dict[str, Any]) -> pd.DataFrame:
    output = df.copy()
    index_settings = settings.get("index", {})
    mean = index_settings.get("index_mean", 100)
    std = index_settings.get("index_std", 15)
    output["violent_index"] = mean + std * output["violent_z_score"]
    output["property_index"] = mean + std * output["property_z_score"]
    output["total_index"] = mean + std * output["total_z_score"]
    composite_z = (
        index_settings.get("violent_weight", 0.60) * output["violent_z_score"]
        + index_settings.get("property_weight", 0.35) * output["property_z_score"]
        + index_settings.get("total_weight", 0.05) * output["total_z_score"]
    )
    output["composite_index"] = mean + std * composite_z
    return output


def _clip_series(series: pd.Series, lower_pct: float, upper_pct: float) -> pd.Series:
    if series.dropna().empty:
        return series
    lower = series.quantile(lower_pct)
    upper = series.quantile(upper_pct)
    return series.clip(lower=lower, upper=upper)


def _percent_rank(series: pd.Series, method: str = "average") -> pd.Series:
    result = pd.Series(index=series.index, dtype="float64")
    valid = series.dropna()
    n = len(valid)
    if n == 0:
        return result
    if valid.max() == 0 and valid.min() == 0:
        result.loc[valid.index] = 0.0
        return result
    if n == 1:
        result.loc[valid.index] = 0.5
        return result
    ranks = valid.rank(method=method, ascending=True)
    result.loc[valid.index] = (ranks - 1) / (n - 1)
    return result.clip(lower=0, upper=1)


def _z_score(series: pd.Series) -> pd.Series:
    result = pd.Series(index=series.index, dtype="float64")
    valid = series.dropna()
    if valid.empty:
        return result
    std = valid.std(ddof=0)
    if std == 0 or pd.isna(std):
        result.loc[valid.index] = 0.0
    else:
        result.loc[valid.index] = (valid - valid.mean()) / std
    return result


def _winsorized_column(rate_column: str) -> str:
    return rate_column.replace("_rate_per_1000", "_rate_winsorized_per_1000")


def _rate_base(rate_column: str) -> str:
    return rate_column.replace("_rate_winsorized_per_1000", "").replace("_rate_per_1000", "")


def _index_columns() -> list[str]:
    return [
        "zcta",
        "year",
        "comparison_scope",
        "comparison_scope_value",
        "population_total",
        *COUNT_COLUMNS,
        "total_rate_per_1000",
        "violent_rate_per_1000",
        "property_rate_per_1000",
        "drug_rate_per_1000",
        "public_order_rate_per_1000",
        "weapons_rate_per_1000",
        "other_rate_per_1000",
        "unknown_rate_per_1000",
        *WINSORIZED_COLUMNS,
        *SCORE_COLUMNS,
        "overall_crime_score_0_100",
        *PERCENTILE_COLUMNS,
        "overall_percentile",
        *Z_SCORE_COLUMNS,
        "violent_index",
        "property_index",
        "total_index",
        "composite_index",
        "percentile_rank",
        "coverage_status",
        "data_source_type",
        "source_names",
        "source_count",
        "assigned_incident_count",
        "spatial_incident_count",
        "is_modeled",
        "observed_level",
        "county_fips",
        "county_name",
        "county_count",
        "county_components",
        "allocation_method",
        "data_coverage_score",
        "confidence_grade",
        "score_notes",
        "violent_score_label",
        "property_score_label",
        "drug_score_label",
        "public_order_score_label",
        "weapons_score_label",
        "other_score_label",
        "total_crime_score_label",
        "overall_crime_score_label",
        "created_at",
    ]


def _ensure_index_columns(df: pd.DataFrame) -> pd.DataFrame:
    output = df.copy()
    defaults: dict[str, object] = {
        "observed_level": "",
        "county_fips": "",
        "county_name": "",
        "county_count": 0,
        "county_components": "",
        "allocation_method": "",
    }
    for column, default in defaults.items():
        if column not in output:
            output[column] = default
        else:
            output[column] = output[column].fillna(default)
    if "coverage_status" in output:
        missing_observed_level = output["observed_level"].astype(str).str.strip() == ""
        output.loc[missing_observed_level & (output["coverage_status"] == "observed"), "observed_level"] = "zcta"
        output.loc[missing_observed_level & (output["coverage_status"] == "national_modeled"), "observed_level"] = ""
    output["county_count"] = pd.to_numeric(output["county_count"], errors="coerce").fillna(0).astype("int64")
    return output


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
