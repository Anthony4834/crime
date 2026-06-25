from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from crime_index.db import get_connection, init_db
from crime_index.normalize.locations import clean_zip
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)


def load_population(
    file_path: str | Path,
    year: int,
    database_path: str | Path | None = None,
) -> int:
    init_db(database_path)
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Population file not found: {path}")

    raw = read_population_file(path)
    normalized = normalize_population(raw, year=year, source=str(path))
    with get_connection(database_path) as con:
        con.execute("DELETE FROM acs_zcta_population WHERE year = ?", [year])
        _insert_df(con, "acs_zcta_population", normalized)
    LOGGER.info("Loaded %s population rows for %s", len(normalized), year)
    return len(normalized)


def read_population_file(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(path, lines=suffix == ".jsonl")
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported population file format: {suffix}")


def normalize_population(df: pd.DataFrame, year: int, source: str) -> pd.DataFrame:
    zcta_col = _first_existing(df, ["zcta", "ZCTA", "zip", "ZIP", "GEOID", "GEO_ID", "NAME"])
    pop_col = _first_existing(df, ["population_total", "population", "B01003_001E"])
    moe_col = _first_existing(df, ["population_margin_error", "margin_error", "B01003_001M"])
    if zcta_col is None or pop_col is None:
        raise ValueError("Population file needs a ZCTA/GEO_ID/NAME column and a population column")

    output = pd.DataFrame()
    output["zcta"] = df[zcta_col].map(clean_zip)
    output["year"] = year
    output["population_total"] = pd.to_numeric(df[pop_col], errors="coerce").astype("Int64")
    if moe_col:
        output["population_margin_error"] = pd.to_numeric(df[moe_col], errors="coerce").astype("Int64")
    else:
        output["population_margin_error"] = pd.Series([pd.NA] * len(df), dtype="Int64")
    output["source"] = source
    output["loaded_at"] = utc_now_naive()
    output = output.dropna(subset=["zcta"]).drop_duplicates(subset=["zcta", "year"], keep="last")
    return output.where(pd.notna(output), None)


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    lower_lookup = {column.lower(): column for column in df.columns}
    for name in names:
        if name in df.columns:
            return name
        if name.lower() in lower_lookup:
            return lower_lookup[name.lower()]
    return None


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    con.execute(f"INSERT INTO {table} SELECT * FROM _insert_df")
    con.unregister("_insert_df")
