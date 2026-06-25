from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import geopandas as gpd
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.normalize.locations import clean_zip

LOGGER = logging.getLogger(__name__)


def load_geography(
    file_path: str | Path,
    year: int,
    database_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    init_db(database_path)
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Geography file not found: {path}")

    settings = settings or load_settings()
    gdf = read_geography(path, settings)
    normalized = normalize_zcta_geometries(gdf, path, year, settings)
    with get_connection(database_path) as con:
        con.execute("DELETE FROM zcta_geometries")
        _insert_df(con, "zcta_geometries", normalized)
    LOGGER.info("Loaded %s ZCTA geometries for %s", len(normalized), year)
    return len(normalized)


def read_geography(path: str | Path, settings: dict[str, Any]) -> gpd.GeoDataFrame:
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        gdf = gpd.read_parquet(path)
    else:
        gdf = gpd.read_file(path)
    canonical_crs = settings.get("geography", {}).get("canonical_crs", "EPSG:4326")
    if gdf.crs is None:
        gdf = gdf.set_crs(canonical_crs)
    else:
        gdf = gdf.to_crs(canonical_crs)
    return gdf


def normalize_zcta_geometries(
    gdf: gpd.GeoDataFrame,
    source_file: str | Path,
    year: int,
    settings: dict[str, Any],
) -> pd.DataFrame:
    zcta_col = _find_zcta_column(gdf, settings)
    if zcta_col is None:
        raise ValueError("Could not find a ZCTA identifier column in geography file")

    rows: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        zcta = clean_zip(row.get(zcta_col))
        if zcta is None:
            continue
        centroid = geometry.centroid
        rows.append(
            {
                "zcta": zcta,
                "geoid": str(row.get("GEOID") or row.get("GEOID20") or row.get("GEOID10") or zcta),
                "state_fips": _clean_optional(row.get("STATEFP") or row.get("STATEFP20") or row.get("STATEFP10")),
                "land_area": _numeric_or_none(row.get("ALAND") or row.get("ALAND20") or row.get("ALAND10")),
                "water_area": _numeric_or_none(row.get("AWATER") or row.get("AWATER20") or row.get("AWATER10")),
                "centroid_lat": float(centroid.y),
                "centroid_lon": float(centroid.x),
                "geom_wkt": geometry.wkt,
                "source_year": year,
                "source_file": str(source_file),
            }
        )
    return pd.DataFrame(rows).drop_duplicates(subset=["zcta"], keep="last")


def _find_zcta_column(gdf: gpd.GeoDataFrame, settings: dict[str, Any]) -> str | None:
    configured = settings.get("geography", {}).get("zcta_id_columns", [])
    for column in configured:
        if column in gdf.columns:
            return column
    lower_lookup = {column.lower(): column for column in gdf.columns}
    for column in configured:
        if column.lower() in lower_lookup:
            return lower_lookup[column.lower()]
    return None


def _clean_optional(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _numeric_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    con.execute(f"INSERT INTO {table} SELECT * FROM _insert_df")
    con.unregister("_insert_df")
