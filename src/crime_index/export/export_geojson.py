from __future__ import annotations

from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
from shapely import wkt


def export_scores_geojson(
    con: duckdb.DuckDBPyConnection,
    year: int,
    output_path: str | Path,
    comparison_scope: str = "source_universe",
) -> int:
    scores = con.execute(
        """
        SELECT *
        FROM zcta_crime_index
        WHERE year = ? AND comparison_scope = ?
        """,
        [year, comparison_scope],
    ).fetchdf()
    if scores.empty:
        return 0
    geometries = con.execute(
        """
        SELECT zcta, any_value(geom_wkt) AS geom_wkt
        FROM zcta_geometries
        GROUP BY zcta
        """
    ).fetchdf()
    if geometries.empty:
        return 0
    geometries["geometry"] = geometries["geom_wkt"].map(wkt.loads)
    gdf = gpd.GeoDataFrame(geometries[["zcta", "geometry"]], geometry="geometry", crs="EPSG:4326")
    merged = gdf.merge(scores, on="zcta", how="inner")
    if merged.empty:
        return 0
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged = _coerce_timestamps(merged)
    merged.to_file(output_path, driver="GeoJSON")
    return len(merged)


def _coerce_timestamps(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    output = gdf.copy()
    for column in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[column]):
            output[column] = output[column].astype(str)
    return output
