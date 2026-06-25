from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


def build_quality_report(con: duckdb.DuckDBPyConnection, year: int | None = None) -> dict[str, Any]:
    year_filter = "WHERE occurred_year = ?" if year is not None else ""
    params = [year] if year is not None else []
    normalized = con.execute(f"SELECT * FROM normalized_crime_incidents {year_filter}", params).fetchdf()
    assignments = con.execute(
        f"""
        SELECT a.*, coalesce(n.incident_count, 1) AS incident_count
        FROM incident_zcta_assignment a
        JOIN normalized_crime_incidents n USING (incident_id)
        {year_filter.replace("occurred_year", "n.occurred_year")}
        """,
        params,
    ).fetchdf()
    annual = con.execute(
        "SELECT * FROM zcta_crime_annual WHERE year = ?" if year is not None else "SELECT * FROM zcta_crime_annual",
        params,
    ).fetchdf()
    index = con.execute(
        "SELECT * FROM zcta_crime_index WHERE year = ?" if year is not None else "SELECT * FROM zcta_crime_index",
        params,
    ).fetchdf()

    normalized_counted = _incident_total(normalized)
    assigned = _incident_total(assignments[assignments["zcta"].notna()]) if not assignments.empty else 0
    spatial = (
        _incident_total(assignments[assignments["assignment_method"] == "spatial_join"])
        if not assignments.empty
        else 0
    )
    unknown = _incident_total(normalized[normalized["offense_group"] == "unknown"]) if not normalized.empty else 0
    valid_dates = _incident_total(normalized[normalized["occurred_date"].notna()]) if not normalized.empty else 0
    valid_coords = (
        _incident_total(normalized[normalized[["latitude", "longitude"]].notna().all(axis=1)])
        if not normalized.empty
        else 0
    )
    valid_zip = _incident_total(normalized[normalized["zcta_from_zip"].notna()]) if not normalized.empty else 0
    duplicate_incidents = int(normalized["incident_id"].duplicated().sum()) if not normalized.empty else 0
    population_present = int(annual["population_total"].notna().sum()) if not annual.empty else 0

    report: dict[str, Any] = {
        "year": year,
        "record_counts": {
            "normalized_incident_rows": len(normalized),
            "normalized_incident_count": normalized_counted,
            "zcta_assignments": len(assignments),
            "annual_zcta_rows": len(annual),
            "index_rows": len(index),
        },
        "index_scope_counts": _records(
            index.groupby(["comparison_scope", "data_source_type", "is_modelled" if "is_modelled" in index else "is_modeled"])
            .size()
            .reset_index(name="count")
        )
        if not index.empty and "comparison_scope" in index
        else [],
        "completeness": {
            "date_completeness_rate": _rate(valid_dates, normalized_counted),
            "coordinate_completeness_rate": _rate(valid_coords, normalized_counted),
            "zip_completeness_rate": _rate(valid_zip, normalized_counted),
            "zcta_assignment_rate": _rate(assigned, normalized_counted),
            "spatial_assignment_rate": _rate(spatial, normalized_counted),
            "offense_classification_rate": _rate(normalized_counted - unknown, normalized_counted),
            "population_coverage_rate": _rate(population_present, len(annual)),
        },
        "duplicates": {
            "duplicate_incident_id_count": duplicate_incidents,
            "duplicate_rate": _rate(duplicate_incidents, len(normalized)),
        },
        "assignment_method_counts": _value_counts(assignments, "assignment_method"),
        "assignment_method_incident_counts": _weighted_value_counts(assignments, "assignment_method"),
        "offense_group_counts": _value_counts(normalized, "offense_group"),
        "offense_group_incident_counts": _weighted_value_counts(normalized, "offense_group"),
        "year_coverage": _records(normalized.groupby(["source_name", "occurred_year"]).size().reset_index(name="count"))
        if not normalized.empty
        else [],
        "top_total_rate_outliers": _top_records(annual, "total_rate_per_1000"),
        "top_violent_rate_outliers": _top_records(annual, "violent_rate_per_1000"),
        "top_overall_scores": _top_records(index, "overall_crime_score_0_100"),
        "confidence_grade_counts": _value_counts(index, "confidence_grade"),
        "confidence_grade_counts_by_scope": _records(
            index.groupby(["comparison_scope", "confidence_grade"]).size().reset_index(name="count")
        )
        if not index.empty and "comparison_scope" in index
        else [],
    }
    return report


def write_quality_report(report: dict[str, Any], output_dir: str | Path = "data/processed") -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "quality_report.json"
    md_path = output_dir / "quality_report.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_quality_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def render_quality_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Crime Index Quality Report",
        "",
        f"Year: {report.get('year') or 'all'}",
        "",
        "## Record Counts",
        *_bullet_dict(report.get("record_counts", {})),
        "",
        "## Index Scopes",
        *_table(report.get("index_scope_counts", [])),
        "",
        "## Completeness",
        *_bullet_dict(report.get("completeness", {}), as_percent=True),
        "",
        "## Assignment Methods",
        *_bullet_dict(report.get("assignment_method_counts", {})),
        "",
        "## Assignment Methods By Counted Incidents",
        *_bullet_dict(report.get("assignment_method_incident_counts", {})),
        "",
        "## Offense Groups",
        *_bullet_dict(report.get("offense_group_counts", {})),
        "",
        "## Offense Groups By Counted Incidents",
        *_bullet_dict(report.get("offense_group_incident_counts", {})),
        "",
        "## Confidence Grades",
        *_bullet_dict(report.get("confidence_grade_counts", {})),
        "",
        "## Confidence Grades By Scope",
        *_table(report.get("confidence_grade_counts_by_scope", [])),
        "",
        "## Top Total Rate Outliers",
        *_table(report.get("top_total_rate_outliers", [])),
        "",
        "## Top Overall Scores",
        *_table(report.get("top_overall_scores", [])),
        "",
    ]
    return "\n".join(lines)


def _rate(numerator: int, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def _incident_total(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    if "incident_count" not in df:
        return len(df)
    return int(df["incident_count"].fillna(1).sum())


def _value_counts(df: pd.DataFrame, column: str) -> dict[str, int]:
    if df.empty or column not in df:
        return {}
    return {str(key): int(value) for key, value in df[column].fillna("null").value_counts().to_dict().items()}


def _weighted_value_counts(df: pd.DataFrame, column: str) -> dict[str, int]:
    if df.empty or column not in df:
        return {}
    if "incident_count" not in df:
        return _value_counts(df, column)
    grouped = df.assign(_key=df[column].fillna("null")).groupby("_key")["incident_count"].sum()
    return {str(key): int(value) for key, value in grouped.sort_values(ascending=False).to_dict().items()}


def _top_records(df: pd.DataFrame, column: str, limit: int = 20) -> list[dict[str, Any]]:
    if df.empty or column not in df:
        return []
    columns = [candidate for candidate in ["zcta", "year", column, "population_total", "total_crime_count", "confidence_grade"] if candidate in df]
    return _records(df.sort_values(column, ascending=False, na_position="last")[columns].head(limit))


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return df.where(pd.notna(df), None).to_dict(orient="records")


def _bullet_dict(data: dict[str, Any], as_percent: bool = False) -> list[str]:
    if not data:
        return ["- None"]
    lines = []
    for key, value in data.items():
        display = f"{float(value) * 100:.2f}%" if as_percent else value
        lines.append(f"- {key}: {display}")
    return lines


def _table(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return ["No rows."]
    headers = list(records[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for record in records:
        lines.append("| " + " | ".join(str(record.get(header, "")) for header in headers) + " |")
    return lines
