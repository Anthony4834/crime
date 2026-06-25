from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.export.export_geojson import export_scores_geojson

LOGGER = logging.getLogger(__name__)

CONSUMER_COLUMNS = [
    "zcta",
    "year",
    "comparison_scope",
    "comparison_scope_value",
    "population_total",
    "coverage_status",
    "data_source_type",
    "source_names",
    "source_count",
    "assigned_incident_count",
    "spatial_incident_count",
    "is_modeled",
    "overall_crime_score_0_100",
    "violent_score_0_100",
    "property_score_0_100",
    "drug_score_0_100",
    "public_order_score_0_100",
    "weapons_score_0_100",
    "other_score_0_100",
    "total_crime_score_0_100",
    "overall_percentile",
    "violent_percentile",
    "property_percentile",
    "drug_percentile",
    "public_order_percentile",
    "weapons_percentile",
    "other_percentile",
    "total_crime_percentile",
    "total_crime_count",
    "violent_crime_count",
    "property_crime_count",
    "drug_crime_count",
    "public_order_crime_count",
    "weapons_crime_count",
    "other_crime_count",
    "unknown_crime_count",
    "total_rate_per_1000",
    "violent_rate_per_1000",
    "property_rate_per_1000",
    "drug_rate_per_1000",
    "public_order_rate_per_1000",
    "weapons_rate_per_1000",
    "other_rate_per_1000",
    "data_coverage_score",
    "confidence_grade",
    "score_notes",
    "overall_crime_score_label",
    "violent_score_label",
    "property_score_label",
    "drug_score_label",
    "public_order_score_label",
    "weapons_score_label",
    "other_score_label",
    "total_crime_score_label",
]


def export_outputs(
    year: int,
    database_path: str | Path | None = None,
    output_dir: str | Path = "data/exports",
    comparison_scope: str = "source_universe",
) -> dict[str, str]:
    init_db(database_path)
    settings = load_settings()
    formats = settings.get("exports", {}).get("formats", ["csv", "parquet", "geojson"])
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    with get_connection(database_path) as con:
        df = con.execute(
            """
            SELECT *
            FROM zcta_crime_index
            WHERE year = ? AND comparison_scope = ?
            ORDER BY zcta
            """,
            [year, comparison_scope],
        ).fetchdf()
        if df.empty:
            LOGGER.warning("No index rows available to export for %s / %s", year, comparison_scope)
            return written
        consumer = df[[column for column in CONSUMER_COLUMNS if column in df.columns]].copy()

        suffix = "" if comparison_scope == "source_universe" else f"_{comparison_scope}"
        if "csv" in formats:
            scores_csv = output_dir / f"zcta_crime_scores_{year}{suffix}.csv"
            index_csv = output_dir / f"zcta_crime_index_{year}{suffix}.csv"
            consumer.to_csv(scores_csv, index=False)
            df.to_csv(index_csv, index=False)
            written["scores_csv"] = str(scores_csv)
            written["index_csv"] = str(index_csv)
        if "parquet" in formats:
            scores_parquet = output_dir / f"zcta_crime_scores_{year}{suffix}.parquet"
            index_parquet = output_dir / f"zcta_crime_index_{year}{suffix}.parquet"
            consumer.to_parquet(scores_parquet, index=False)
            df.to_parquet(index_parquet, index=False)
            written["scores_parquet"] = str(scores_parquet)
            written["index_parquet"] = str(index_parquet)
        if "geojson" in formats:
            scores_geojson = output_dir / f"zcta_crime_scores_{year}{suffix}.geojson"
            index_geojson = output_dir / f"zcta_crime_index_{year}{suffix}.geojson"
            rows = export_scores_geojson(con, year, scores_geojson, comparison_scope)
            if rows:
                written["scores_geojson"] = str(scores_geojson)
                # The legacy index name is intentionally the same content for backward compatibility.
                Path(index_geojson).write_text(Path(scores_geojson).read_text(encoding="utf-8"), encoding="utf-8")
                written["index_geojson"] = str(index_geojson)
    LOGGER.info("Exported outputs: %s", written)
    return written
