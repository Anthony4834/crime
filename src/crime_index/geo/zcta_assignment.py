from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import geopandas as gpd
import pandas as pd
from shapely import wkt

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)

STATE_TO_FIPS = {
    "AL": "01",
    "AK": "02",
    "AZ": "04",
    "AR": "05",
    "CA": "06",
    "CO": "08",
    "CT": "09",
    "DE": "10",
    "DC": "11",
    "FL": "12",
    "GA": "13",
    "HI": "15",
    "ID": "16",
    "IL": "17",
    "IN": "18",
    "IA": "19",
    "KS": "20",
    "KY": "21",
    "LA": "22",
    "ME": "23",
    "MD": "24",
    "MA": "25",
    "MI": "26",
    "MN": "27",
    "MS": "28",
    "MO": "29",
    "MT": "30",
    "NE": "31",
    "NV": "32",
    "NH": "33",
    "NJ": "34",
    "NM": "35",
    "NY": "36",
    "NC": "37",
    "ND": "38",
    "OH": "39",
    "OK": "40",
    "OR": "41",
    "PA": "42",
    "RI": "44",
    "SC": "45",
    "SD": "46",
    "TN": "47",
    "TX": "48",
    "UT": "49",
    "VT": "50",
    "VA": "51",
    "WA": "53",
    "WV": "54",
    "WI": "55",
    "WY": "56",
    "PR": "72",
}

STATE_BOUNDS = {
    "AL": (30.1, 35.1, -88.6, -84.8),
    "AK": (51.0, 72.0, -180.0, -129.0),
    "AZ": (31.2, 37.1, -114.9, -109.0),
    "AR": (33.0, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.6, -114.0),
    "CO": (36.9, 41.1, -109.2, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7),
    "DE": (38.4, 39.9, -75.9, -75.0),
    "DC": (38.7, 39.0, -77.2, -76.8),
    "FL": (24.3, 31.1, -87.8, -79.8),
    "GA": (30.2, 35.1, -85.7, -80.7),
    "HI": (18.8, 22.5, -160.4, -154.7),
    "ID": (42.0, 49.1, -117.4, -111.0),
    "IL": (36.9, 42.6, -91.6, -87.0),
    "IN": (37.7, 41.8, -88.2, -84.7),
    "IA": (40.3, 43.6, -96.8, -90.0),
    "KS": (36.9, 40.1, -102.2, -94.5),
    "KY": (36.4, 39.2, -89.7, -81.9),
    "LA": (28.8, 33.1, -94.1, -88.7),
    "ME": (42.9, 47.5, -71.2, -66.8),
    "MD": (37.8, 39.8, -79.6, -75.0),
    "MA": (41.1, 42.9, -73.6, -69.8),
    "MI": (41.6, 48.4, -90.5, -82.1),
    "MN": (43.4, 49.4, -97.3, -89.4),
    "MS": (30.0, 35.1, -91.8, -88.0),
    "MO": (35.9, 40.7, -95.9, -89.0),
    "MT": (44.2, 49.1, -116.2, -104.0),
    "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (35.0, 42.1, -120.1, -114.0),
    "NH": (42.6, 45.4, -72.6, -70.6),
    "NJ": (38.8, 41.4, -75.7, -73.8),
    "NM": (31.2, 37.1, -109.2, -103.0),
    "NY": (40.4, 45.1, -79.9, -71.7),
    "NC": (33.8, 36.7, -84.4, -75.3),
    "ND": (45.8, 49.1, -104.2, -96.4),
    "OH": (38.3, 42.4, -84.9, -80.4),
    "OK": (33.5, 37.1, -103.1, -94.3),
    "OR": (41.9, 46.4, -124.7, -116.4),
    "PA": (39.6, 42.3, -80.7, -74.6),
    "RI": (41.1, 42.1, -71.9, -71.0),
    "SC": (32.0, 35.3, -83.4, -78.4),
    "SD": (42.4, 45.9, -104.2, -96.3),
    "TN": (34.9, 36.8, -90.4, -81.6),
    "TX": (25.6, 36.6, -106.8, -93.4),
    "UT": (36.9, 42.1, -114.2, -108.9),
    "VT": (42.6, 45.1, -73.5, -71.4),
    "VA": (36.5, 39.6, -83.8, -75.1),
    "WA": (45.4, 49.1, -124.9, -116.8),
    "WV": (37.1, 40.7, -82.8, -77.6),
    "WI": (42.4, 47.2, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.2, -104.0),
    "PR": (17.8, 18.6, -67.4, -65.1),
}


def assign_zctas(database_path: str | Path | None = None, settings: dict[str, Any] | None = None) -> dict[str, int]:
    init_db(database_path)
    settings = settings or load_settings()
    invalid_values = settings.get("quality", {}).get("invalid_coordinate_values", [[0, 0]])

    with get_connection(database_path) as con:
        incidents = con.execute(
            """
            SELECT incident_id, jurisdiction_state, latitude, longitude, zcta_from_zip
            FROM normalized_crime_incidents
            """
        ).fetchdf()
        zcta_gdf = load_zcta_geodataframe(con)
        assignments = assign_dataframe_to_zctas(incidents, zcta_gdf, invalid_values)
        con.execute("DELETE FROM incident_zcta_assignment")
        _insert_df(con, "incident_zcta_assignment", assignments)

    summary = assignments["assignment_method"].value_counts(dropna=False).to_dict()
    LOGGER.info("Assigned ZCTAs: %s", summary)
    return {str(key): int(value) for key, value in summary.items()}


def load_zcta_geodataframe(con: duckdb.DuckDBPyConnection) -> gpd.GeoDataFrame:
    zctas = con.execute("SELECT zcta, state_fips, geom_wkt FROM zcta_geometries").fetchdf()
    if zctas.empty:
        return gpd.GeoDataFrame({"zcta": [], "geometry": []}, geometry="geometry", crs="EPSG:4326")
    zctas["geometry"] = zctas["geom_wkt"].map(wkt.loads)
    return gpd.GeoDataFrame(zctas[["zcta", "state_fips", "geometry"]], geometry="geometry", crs="EPSG:4326")


def assign_dataframe_to_zctas(
    incidents: pd.DataFrame,
    zcta_gdf: gpd.GeoDataFrame,
    invalid_coordinate_values: list[list[float]] | None = None,
) -> pd.DataFrame:
    if incidents.empty:
        return pd.DataFrame(columns=_assignment_columns())

    base = incidents.copy()
    base["latitude"] = pd.to_numeric(base["latitude"], errors="coerce")
    base["longitude"] = pd.to_numeric(base["longitude"], errors="coerce")
    base["has_valid_coordinates"] = _valid_coordinate_mask(
        base["latitude"],
        base["longitude"],
        invalid_coordinate_values,
    )
    base["coordinates_outside_expected_state"] = base["has_valid_coordinates"] & ~_state_bounds_mask(base)
    base["has_valid_coordinates"] = base["has_valid_coordinates"] & ~base["coordinates_outside_expected_state"]
    matched_by_incident: dict[str, str] = {}
    zcta_to_state = zcta_gdf.drop_duplicates("zcta").set_index("zcta")["state_fips"].to_dict() if "state_fips" in zcta_gdf else {}
    valid = base[base["has_valid_coordinates"]].copy()
    if not valid.empty and not zcta_gdf.empty:
        points = gpd.GeoDataFrame(
            valid,
            geometry=gpd.points_from_xy(valid["longitude"], valid["latitude"]),
            crs="EPSG:4326",
        )
        try:
            join_columns = ["zcta", "geometry"] + (["state_fips"] if "state_fips" in zcta_gdf else [])
            joined = gpd.sjoin(points, zcta_gdf[join_columns], how="left", predicate="within")
            matches = joined.dropna(subset=["zcta"])
            matched_by_incident = matches.drop_duplicates(subset=["incident_id"]).set_index("incident_id")["zcta"].to_dict()
        except Exception as exc:
            LOGGER.warning("GeoPandas spatial join failed, falling back to row-wise contains checks: %s", exc)
            matched_by_incident = _manual_spatial_join(points, zcta_gdf)

    now = utc_now_naive()
    zip_fallback = base["zcta_from_zip"].map(_clean_str)
    valid_zctas = set(zcta_to_state.keys()) if zcta_to_state else set(zcta_gdf["zcta"].dropna().astype(str))
    zip_exists = zip_fallback.isin(valid_zctas) if valid_zctas else zip_fallback.notna()
    assignments = pd.DataFrame(
        {
            "incident_id": base["incident_id"],
            "zcta": base["incident_id"].map(matched_by_incident),
            "assignment_method": "unassigned",
            "assignment_confidence": "none",
            "assignment_notes": "missing_coordinates_and_zip",
            "assigned_at": now,
        }
    )
    spatial_mask = assignments["zcta"].notna()
    valid_outside_mask = ~spatial_mask & base["has_valid_coordinates"]
    zip_mask = ~spatial_mask & ~base["has_valid_coordinates"] & zip_fallback.notna() & zip_exists
    invalid_zip_mask = ~spatial_mask & ~base["has_valid_coordinates"] & zip_fallback.notna() & ~zip_exists
    outside_state_mask = ~spatial_mask & base["coordinates_outside_expected_state"] & ~zip_mask

    assignments.loc[spatial_mask, "assignment_method"] = "spatial_join"
    assignments.loc[spatial_mask, "assignment_confidence"] = "high"
    assignments.loc[spatial_mask, "assignment_notes"] = None

    assignments.loc[valid_outside_mask, "assignment_notes"] = "valid_coordinates_outside_loaded_zctas"
    assignments.loc[outside_state_mask, "assignment_notes"] = "valid_coordinates_outside_expected_state"
    assignments.loc[invalid_zip_mask, "assignment_notes"] = "zip_fallback_without_loaded_zcta_geometry"

    assignments.loc[zip_mask, "zcta"] = zip_fallback.loc[zip_mask]
    assignments.loc[zip_mask, "assignment_method"] = "zip_fallback"
    assignments.loc[zip_mask, "assignment_confidence"] = "medium"
    assignments.loc[zip_mask, "assignment_notes"] = "missing_or_invalid_coordinates_used_zip"
    assignments = _apply_state_sanity_check(assignments, base, zcta_to_state)
    return assignments[_assignment_columns()]


def _apply_state_sanity_check(
    assignments: pd.DataFrame,
    incidents: pd.DataFrame,
    zcta_to_state: dict[str, str],
) -> pd.DataFrame:
    if not zcta_to_state or "jurisdiction_state" not in incidents:
        return assignments
    output = assignments.copy()
    expected_fips = incidents["jurisdiction_state"].map(lambda value: STATE_TO_FIPS.get(str(value).upper()) if not pd.isna(value) else None)
    assigned_fips = output["zcta"].map(zcta_to_state)
    mismatch = output["zcta"].notna() & expected_fips.notna() & assigned_fips.notna() & (expected_fips != assigned_fips)
    spatial_mismatch = mismatch & (output["assignment_method"] == "spatial_join")
    zip_mismatch = mismatch & (output["assignment_method"] == "zip_fallback")
    output.loc[mismatch, "zcta"] = None
    output.loc[mismatch, "assignment_method"] = "unassigned"
    output.loc[mismatch, "assignment_confidence"] = "none"
    output.loc[spatial_mismatch, "assignment_notes"] = "valid_coordinates_outside_expected_state"
    output.loc[zip_mismatch, "assignment_notes"] = "zip_fallback_outside_expected_state"
    return output


def _valid_coordinate_mask(
    latitude: pd.Series,
    longitude: pd.Series,
    invalid_coordinate_values: list[list[float]] | None,
) -> pd.Series:
    valid = latitude.notna() & longitude.notna() & latitude.between(-90, 90) & longitude.between(-180, 180)
    for invalid_lat, invalid_lon in invalid_coordinate_values or [[0, 0]]:
        valid &= ~((latitude == invalid_lat) & (longitude == invalid_lon))
    return valid


def _state_bounds_mask(incidents: pd.DataFrame) -> pd.Series:
    if "jurisdiction_state" not in incidents:
        return pd.Series(True, index=incidents.index)
    output = pd.Series(True, index=incidents.index)
    for state, bounds in STATE_BOUNDS.items():
        state_mask = incidents["jurisdiction_state"].astype("string").str.upper() == state
        if not state_mask.any():
            continue
        min_lat, max_lat, min_lon, max_lon = bounds
        output.loc[state_mask] = (
            incidents.loc[state_mask, "latitude"].between(min_lat, max_lat)
            & incidents.loc[state_mask, "longitude"].between(min_lon, max_lon)
        )
    return output


def _manual_spatial_join(points: gpd.GeoDataFrame, zcta_gdf: gpd.GeoDataFrame) -> dict[str, str]:
    matches: dict[str, str] = {}
    for point_row in points.itertuples():
        point = point_row.geometry
        for zcta_row in zcta_gdf.itertuples():
            if point.within(zcta_row.geometry):
                matches[point_row.incident_id] = zcta_row.zcta
                break
    return matches


def _clean_str(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _assignment_columns() -> list[str]:
    return [
        "incident_id",
        "zcta",
        "assignment_method",
        "assignment_confidence",
        "assignment_notes",
        "assigned_at",
    ]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    con.execute(f"INSERT INTO {table} SELECT * FROM _insert_df")
    con.unregister("_insert_df")
