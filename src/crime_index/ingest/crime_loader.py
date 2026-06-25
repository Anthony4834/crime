from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.config import load_sources
from crime_index.config import select_sources
from crime_index.db import get_connection, init_db
from crime_index.normalize.dates import parse_occurred_at, parse_year_day_time
from crime_index.normalize.locations import to_float
from crime_index.utils.file_utils import file_sha256
from crime_index.utils.text_utils import clean_text
from crime_index.utils.time_utils import utc_from_timestamp_naive, utc_now_naive

LOGGER = logging.getLogger(__name__)


def ingest_crime(
    sources_config_path: str | Path = "config/sources.yaml",
    database_path: str | Path | None = None,
    source_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    init_db(database_path)
    sources = select_sources(load_sources(sources_config_path), source_names)
    results: dict[str, int] = {}
    with get_connection(database_path) as con:
        if source_names:
            _delete_sources(con, list(sources))
        else:
            con.execute("DELETE FROM raw_crime_files")
            con.execute("DELETE FROM raw_crime_records")
            con.execute("DELETE FROM staged_crime_incidents")
        for source_name, source_config in sources.items():
            row_count = ingest_source(con, source_name, source_config)
            results[source_name] = row_count
            LOGGER.info("Ingested %s rows for source %s", row_count, source_name)
    return results


def ingest_source(
    con: duckdb.DuckDBPyConnection,
    source_name: str,
    source_config: dict[str, Any],
) -> int:
    path = Path(source_config["file"])
    if not path.exists():
        raise FileNotFoundError(f"Crime source not found: {path}")

    df = read_tabular_file(path)
    now = utc_now_naive()
    file_hash = file_sha256(path)
    raw_file_id = file_hash
    stat = path.stat()

    file_meta = pd.DataFrame(
        [
            {
                "raw_file_id": raw_file_id,
                "source_name": source_name,
                "file_path": str(path),
                "file_format": path.suffix.lower().lstrip("."),
                "file_size_bytes": stat.st_size,
                "file_modified_at": utc_from_timestamp_naive(stat.st_mtime),
                "ingested_at": now,
                "row_count": len(df),
                "file_hash": file_hash,
            }
        ]
    )
    _insert_df(con, "raw_crime_files", file_meta)

    raw_records = pd.DataFrame(
        {
            "raw_record_id": [
                _stable_hash(source_name, raw_file_id, str(row_number))
                for row_number in range(1, len(df) + 1)
            ],
            "raw_file_id": raw_file_id,
            "source_name": source_name,
            "source_row_number": range(1, len(df) + 1),
            "raw_payload_json": _json_payloads(df),
            "ingested_at": now,
        }
    )
    _insert_df(con, "raw_crime_records", raw_records)

    staged = build_staged_incidents(df, source_name, source_config, now)
    _insert_df(con, "staged_crime_incidents", staged)
    return len(df)


def read_tabular_file(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(path, lines=suffix == ".jsonl")
    if suffix == ".geojson":
        import geopandas as gpd

        gdf = gpd.read_file(path)
        df = pd.DataFrame(gdf.drop(columns="geometry", errors="ignore"))
        if "geometry" in gdf:
            points = gdf.geometry
            df["geometry_wkt"] = points.to_wkt()
            if points.geom_type.isin(["Point"]).all():
                df["longitude"] = points.x
                df["latitude"] = points.y
        return df
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported crime source format: {suffix}")


def build_staged_incidents(
    df: pd.DataFrame,
    source_name: str,
    source_config: dict[str, Any],
    loaded_at: datetime | None = None,
) -> pd.DataFrame:
    loaded_at = loaded_at or utc_now_naive()
    row_numbers = pd.Series(range(1, len(df) + 1), index=df.index)
    source_incident_id = _column(df, source_config.get("incident_id_column"))
    incident_count = _incident_count_series(_column(df, source_config.get("count_column")))
    occurred_at = _resolve_occurred_at_series(df, source_config)
    offense_raw = _coalesce_configured_columns(
        df,
        source_config.get("offense_column"),
        source_config.get("offense_fallback_columns"),
    )
    offense_code_raw = _column(df, source_config.get("offense_code_column"))
    address_raw = _column(df, source_config.get("address_column"))
    zip_raw = _column(df, source_config.get("zip_column"))
    latitude_raw, longitude_raw = _resolve_coordinates_series(df, source_config)
    incident_ids = _incident_id_series(
        source_name,
        source_incident_id,
        row_numbers,
        occurred_at,
        offense_raw,
        latitude_raw,
        longitude_raw,
        address_raw,
    )
    occurred_dt = pd.to_datetime(occurred_at, errors="coerce")
    output = pd.DataFrame(
        {
            "incident_id": incident_ids,
            "source_name": source_name,
            "jurisdiction_name": source_config.get("jurisdiction_name"),
            "jurisdiction_state": source_config.get("jurisdiction_state"),
            "source_row_number": row_numbers.astype("int64"),
            "source_incident_id": source_incident_id.map(clean_text),
            "incident_count": incident_count,
            "occurred_at": occurred_dt,
            "occurred_date": occurred_dt.dt.date,
            "occurred_year": occurred_dt.dt.year.astype("Int64"),
            "occurred_month": occurred_dt.dt.month.astype("Int64"),
            "offense_raw": offense_raw.map(clean_text),
            "offense_code_raw": offense_code_raw.map(clean_text),
            "address_raw": address_raw.map(clean_text),
            "zip_raw": zip_raw.map(clean_text),
            "latitude_raw": latitude_raw,
            "longitude_raw": longitude_raw,
            "source_crs": source_config.get("source_crs", "EPSG:4326"),
            "loaded_at": loaded_at,
        }
    )
    output.loc[occurred_dt.isna(), ["occurred_at", "occurred_date", "occurred_year", "occurred_month"]] = None
    return output[_staged_columns()]


def _value(record: pd.Series, column: str | None) -> Any:
    if not column or column not in record:
        return None
    value = record[column]
    if pd.isna(value):
        return None
    return value


def _column(df: pd.DataFrame, column: str | None) -> pd.Series:
    if not column or column not in df:
        return pd.Series([None] * len(df), index=df.index, dtype="object")
    return df[column]


def _coalesce_configured_columns(
    df: pd.DataFrame,
    primary_column: str | None,
    fallback_columns: list[str] | None,
) -> pd.Series:
    columns = [primary_column, *(fallback_columns or [])]
    output = pd.Series([None] * len(df), index=df.index, dtype="object")
    for column in columns:
        values = _column(df, column).map(clean_text)
        output = output.where(output.map(clean_text).notna(), values)
    return output


def _incident_count_series(series: pd.Series) -> pd.Series:
    counts = pd.to_numeric(series, errors="coerce").fillna(1)
    counts = counts.where(counts > 0, 1)
    return counts.round().astype("int64")


def _resolve_occurred_at(record: pd.Series, source_config: dict[str, Any]) -> pd.Timestamp | None:
    if source_config.get("year_column") and source_config.get("day_of_year_column"):
        parsed = parse_year_day_time(
            _value(record, source_config.get("year_column")),
            _value(record, source_config.get("day_of_year_column")),
            _value(record, source_config.get("time_column")),
        )
        if parsed is not None:
            return parsed
    return parse_occurred_at(
        _value(record, source_config.get("date_column")),
        _value(record, source_config.get("time_column")),
        source_config.get("epoch_timezone"),
    )


def _resolve_occurred_at_series(df: pd.DataFrame, source_config: dict[str, Any]) -> pd.Series:
    if source_config.get("year_column") and source_config.get("day_of_year_column"):
        years = _column(df, source_config.get("year_column"))
        days = _column(df, source_config.get("day_of_year_column"))
        times = _column(df, source_config.get("time_column"))
        return pd.Series(
            [parse_year_day_time(year, day, time) for year, day, time in zip(years, days, times)],
            index=df.index,
            dtype="object",
        )
    dates = _column(df, source_config.get("date_column"))
    times = _column(df, source_config.get("time_column"))
    epoch_timezone = source_config.get("epoch_timezone")
    return pd.Series(
        [parse_occurred_at(date, time, epoch_timezone) for date, time in zip(dates, times)],
        index=df.index,
        dtype="object",
    )


def _resolve_coordinates(record: pd.Series, source_config: dict[str, Any]) -> tuple[float | None, float | None]:
    latitude_raw = to_float(_value(record, source_config.get("latitude_column")))
    longitude_raw = to_float(_value(record, source_config.get("longitude_column")))
    if latitude_raw is not None and longitude_raw is not None:
        return latitude_raw, longitude_raw
    point_column = source_config.get("geocoded_point_column")
    point_value = _value(record, point_column)
    parsed = _parse_point_wkt(point_value)
    if parsed is not None:
        return parsed
    return latitude_raw, longitude_raw


def _resolve_coordinates_series(df: pd.DataFrame, source_config: dict[str, Any]) -> tuple[pd.Series, pd.Series]:
    latitude = _column(df, source_config.get("latitude_column")).map(to_float)
    longitude = _column(df, source_config.get("longitude_column")).map(to_float)
    point_column = source_config.get("geocoded_point_column")
    if point_column and point_column in df:
        parsed_points = _column(df, point_column).map(_parse_point_wkt)
        parsed_latitude = parsed_points.map(lambda point: point[0] if point is not None else None)
        parsed_longitude = parsed_points.map(lambda point: point[1] if point is not None else None)
        missing_coordinate = latitude.isna() | longitude.isna()
        latitude = latitude.where(~missing_coordinate, parsed_latitude)
        longitude = longitude.where(~missing_coordinate, parsed_longitude)
    return latitude, longitude


def _parse_point_wkt(value: Any) -> tuple[float, float] | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.match(r"POINT\s*\(\s*([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s*\)", text, flags=re.IGNORECASE)
    if not match:
        return None
    longitude = to_float(match.group(1))
    latitude = to_float(match.group(2))
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def _json_payload(record: dict[str, Any]) -> str:
    def clean(value: Any) -> Any:
        if pd.isna(value):
            return None
        return value

    return json.dumps({key: clean(value) for key, value in record.items()}, default=str, sort_keys=True)


def _json_payloads(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    return df.to_json(orient="records", lines=True, date_format="iso").splitlines()


def _incident_id(
    source_name: str,
    source_incident_id: Any,
    row_number: int,
    occurred_at: Any,
    offense_raw: Any,
    latitude_raw: Any,
    longitude_raw: Any,
    address_raw: Any,
) -> str:
    if clean_text(source_incident_id):
        return _stable_hash(source_name, str(source_incident_id))
    return _stable_hash(
        source_name,
        str(row_number),
        str(occurred_at),
        str(offense_raw),
        str(latitude_raw),
        str(longitude_raw),
        str(address_raw),
    )


def _incident_id_series(
    source_name: str,
    source_incident_id: pd.Series,
    row_numbers: pd.Series,
    occurred_at: pd.Series,
    offense_raw: pd.Series,
    latitude_raw: pd.Series,
    longitude_raw: pd.Series,
    address_raw: pd.Series,
) -> list[str]:
    incident_ids: list[str] = []
    cleaned_source_ids = source_incident_id.map(clean_text)
    for source_id, cleaned_source_id, row_number, occurred, offense, latitude, longitude, address in zip(
        source_incident_id,
        cleaned_source_ids,
        row_numbers,
        occurred_at,
        offense_raw,
        latitude_raw,
        longitude_raw,
        address_raw,
    ):
        if cleaned_source_id:
            incident_ids.append(_stable_hash(source_name, str(source_id)))
            continue
        incident_ids.append(
            _stable_hash(
                source_name,
                str(row_number),
                str(occurred),
                str(offense),
                str(latitude),
                str(longitude),
                str(address),
            )
        )
    return incident_ids


def _stable_hash(*parts: str) -> str:
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _staged_columns() -> list[str]:
    return [
        "incident_id",
        "source_name",
        "jurisdiction_name",
        "jurisdiction_state",
        "source_row_number",
        "source_incident_id",
        "incident_count",
        "occurred_at",
        "occurred_date",
        "occurred_year",
        "occurred_month",
        "offense_raw",
        "offense_code_raw",
        "address_raw",
        "zip_raw",
        "latitude_raw",
        "longitude_raw",
        "source_crs",
        "loaded_at",
    ]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")


def _delete_sources(con: duckdb.DuckDBPyConnection, source_names: list[str]) -> None:
    if not source_names:
        return
    placeholders = ", ".join("?" for _ in source_names)
    for table in ["raw_crime_files", "raw_crime_records", "staged_crime_incidents"]:
        con.execute(f"DELETE FROM {table} WHERE source_name IN ({placeholders})", source_names)
