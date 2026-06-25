from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_offense_mapping, load_settings
from crime_index.db import get_connection, init_db
from crime_index.normalize.locations import clean_zip, is_valid_lat_lon, point_wkt, to_float
from crime_index.normalize.offense_classifier import classify_offense
from crime_index.utils.text_utils import clean_text, normalize_for_match
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)


def normalize_crime(database_path: str | Path | None = None, settings: dict[str, Any] | None = None) -> int:
    init_db(database_path)
    settings = settings or load_settings()
    mapping = load_offense_mapping()
    invalid_values = settings.get("quality", {}).get("invalid_coordinate_values", [[0, 0]])

    with get_connection(database_path) as con:
        staged = con.execute("SELECT * FROM staged_crime_incidents").fetchdf()
        con.execute("DELETE FROM normalized_crime_incidents")
        normalized = normalize_staged_dataframe(staged, mapping, invalid_values)
        _insert_df(con, "normalized_crime_incidents", normalized)

    LOGGER.info("Normalized %s crime incidents", len(normalized))
    return len(normalized)


def normalize_staged_dataframe(
    staged: pd.DataFrame,
    offense_mapping: dict[str, Any] | None = None,
    invalid_coordinate_values: list[list[float]] | None = None,
) -> pd.DataFrame:
    if staged.empty:
        return pd.DataFrame(columns=_normalized_columns())

    now = utc_now_naive()
    occurred_date = _series(staged, "occurred_date")
    offense_raw = _series(staged, "offense_raw")
    zip_raw = _series(staged, "zip_raw")
    latitude_raw = _series(staged, "latitude_raw")
    longitude_raw = _series(staged, "longitude_raw")

    classification = _classify_offense_series(offense_raw, offense_mapping)
    zcta_from_zip = _map_cached(zip_raw, clean_zip)
    latitude = pd.to_numeric(latitude_raw, errors="coerce")
    longitude = pd.to_numeric(longitude_raw, errors="coerce")
    valid_coordinates = _valid_coordinate_mask(latitude, longitude, invalid_coordinate_values)

    output = pd.DataFrame(
        {
            "incident_id": _series(staged, "incident_id"),
            "source_name": _series(staged, "source_name"),
            "incident_count": pd.to_numeric(_series(staged, "incident_count"), errors="coerce").fillna(1).astype("int64"),
            "jurisdiction_name": _series(staged, "jurisdiction_name"),
            "jurisdiction_state": _series(staged, "jurisdiction_state"),
            "occurred_at": _series(staged, "occurred_at"),
            "occurred_date": occurred_date,
            "occurred_year": pd.to_numeric(_series(staged, "occurred_year"), errors="coerce").astype("Int64"),
            "occurred_month": pd.to_numeric(_series(staged, "occurred_month"), errors="coerce").astype("Int64"),
            "offense_raw": _map_cached(offense_raw, clean_text),
            "offense_normalized": classification["offense_normalized"],
            "offense_group": classification["offense_group"],
            "offense_subgroup": classification["offense_subgroup"],
            "is_violent": classification["offense_group"] == "violent",
            "is_property": classification["offense_group"] == "property",
            "address_normalized": _map_cached(_series(staged, "address_raw"), normalize_for_match),
            "zip_raw": _map_cached(zip_raw, clean_text),
            "zcta_from_zip": zcta_from_zip,
            "latitude": latitude.where(valid_coordinates),
            "longitude": longitude.where(valid_coordinates),
            "geom_wkt": _point_wkt_series(latitude, longitude, valid_coordinates),
            "data_quality_score": _data_quality_score_series(
                occurred_date,
                classification["offense_group"],
                valid_coordinates,
                zcta_from_zip,
            ),
            "normalization_notes": _normalization_notes_series(
                occurred_date,
                classification["offense_group"],
                valid_coordinates,
                zcta_from_zip,
            ),
            "created_at": now,
        }
    )
    return output[_normalized_columns()]


def _series(df: pd.DataFrame, column: str) -> pd.Series:
    if column in df:
        return df[column]
    return pd.Series(pd.NA, index=df.index)


def _classify_offense_series(offense_raw: pd.Series, offense_mapping: dict[str, Any] | None) -> pd.DataFrame:
    keys = offense_raw.map(_cache_key)
    classifications = {
        key: classify_offense(None if key is None else key, offense_mapping)
        for key in keys.drop_duplicates()
    }
    classified = keys.map(classifications)
    return pd.DataFrame(
        {
            "offense_normalized": classified.map(lambda value: value.offense_normalized),
            "offense_group": classified.map(lambda value: value.offense_group),
            "offense_subgroup": classified.map(lambda value: value.offense_subgroup),
        },
        index=offense_raw.index,
    )


def _map_cached(series: pd.Series, func: Any) -> pd.Series:
    cache: dict[str | None, Any] = {}

    def cached(value: Any) -> Any:
        key = _cache_key(value)
        if key not in cache:
            cache[key] = func(None if key is None else key)
        return cache[key]

    return series.map(cached)


def _cache_key(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value)


def _valid_coordinate_mask(
    latitude: pd.Series,
    longitude: pd.Series,
    invalid_coordinate_values: list[list[float]] | None,
) -> pd.Series:
    valid = latitude.notna() & longitude.notna() & latitude.between(-90, 90) & longitude.between(-180, 180)
    for invalid_lat, invalid_lon in invalid_coordinate_values or [[0, 0]]:
        valid &= ~((latitude == invalid_lat) & (longitude == invalid_lon))
    return valid


def _point_wkt_series(latitude: pd.Series, longitude: pd.Series, valid_coordinates: pd.Series) -> pd.Series:
    output = pd.Series(pd.NA, index=latitude.index, dtype="object")
    output.loc[valid_coordinates] = (
        "POINT ("
        + longitude.loc[valid_coordinates].astype(str)
        + " "
        + latitude.loc[valid_coordinates].astype(str)
        + ")"
    )
    return output


def _data_quality_score_series(
    occurred_date: pd.Series,
    offense_group: pd.Series,
    valid_coordinates: pd.Series,
    zcta_from_zip: pd.Series,
) -> pd.Series:
    score = (
        occurred_date.notna().astype(float)
        + (offense_group != "unknown").astype(float)
        + valid_coordinates.astype(float)
        + zcta_from_zip.notna().astype(float)
    ) / 4
    return score.round(3)


def _normalization_notes_series(
    occurred_date: pd.Series,
    offense_group: pd.Series,
    valid_coordinates: pd.Series,
    zcta_from_zip: pd.Series,
) -> pd.Series:
    notes = pd.Series("", index=occurred_date.index, dtype="object")
    notes = _append_note_where(notes, occurred_date.isna(), "missing_or_invalid_date")
    notes = _append_note_where(notes, offense_group == "unknown", "unknown_offense")
    notes = _append_note_where(notes, ~valid_coordinates, "missing_or_invalid_coordinates")
    notes = _append_note_where(notes, zcta_from_zip.isna(), "missing_or_invalid_zip")
    return notes.replace("", pd.NA)


def _append_note_where(notes: pd.Series, mask: pd.Series, note: str) -> pd.Series:
    output = notes.copy()
    empty = output.eq("")
    output.loc[mask & empty] = note
    output.loc[mask & ~empty] = output.loc[mask & ~empty] + "; " + note
    return output


def _normalization_notes(record: pd.Series, offense_group: str, valid_coordinates: bool, zcta_from_zip: str | None) -> list[str]:
    return _normalization_notes_from_values(record.get("occurred_date"), offense_group, valid_coordinates, zcta_from_zip)


def _normalization_notes_from_values(
    occurred_date: Any,
    offense_group: str,
    valid_coordinates: bool,
    zcta_from_zip: str | None,
) -> list[str]:
    notes: list[str] = []
    if pd.isna(occurred_date):
        notes.append("missing_or_invalid_date")
    if offense_group == "unknown":
        notes.append("unknown_offense")
    if not valid_coordinates:
        notes.append("missing_or_invalid_coordinates")
    if zcta_from_zip is None:
        notes.append("missing_or_invalid_zip")
    return notes


def _data_quality_score(record: pd.Series, offense_group: str, valid_coordinates: bool, zcta_from_zip: str | None) -> float:
    return _data_quality_score_from_values(record.get("occurred_date"), offense_group, valid_coordinates, zcta_from_zip)


def _data_quality_score_from_values(
    occurred_date: Any,
    offense_group: str,
    valid_coordinates: bool,
    zcta_from_zip: str | None,
) -> float:
    components = [
        1.0 if not pd.isna(occurred_date) else 0.0,
        1.0 if offense_group != "unknown" else 0.0,
        1.0 if valid_coordinates else 0.0,
        1.0 if zcta_from_zip is not None else 0.0,
    ]
    return round(sum(components) / len(components), 3)


def _nullable(value: Any) -> Any:
    return None if pd.isna(value) else value


def _int_or_none(value: Any) -> int | None:
    if pd.isna(value):
        return None
    return int(value)


def _normalized_columns() -> list[str]:
    return [
        "incident_id",
        "source_name",
        "incident_count",
        "jurisdiction_name",
        "jurisdiction_state",
        "occurred_at",
        "occurred_date",
        "occurred_year",
        "occurred_month",
        "offense_raw",
        "offense_normalized",
        "offense_group",
        "offense_subgroup",
        "is_violent",
        "is_property",
        "address_normalized",
        "zip_raw",
        "zcta_from_zip",
        "latitude",
        "longitude",
        "geom_wkt",
        "data_quality_score",
        "normalization_notes",
        "created_at",
    ]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
