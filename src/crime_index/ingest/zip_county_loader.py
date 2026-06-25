from __future__ import annotations

import logging
import re
from pathlib import Path

import duckdb
import pandas as pd

from crime_index.db import get_connection, init_db
from crime_index.normalize.locations import clean_zip
from crime_index.utils.text_utils import normalize_for_match
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)

STATE_FIPS_TO_CODE = {
    "01": "AL",
    "02": "AK",
    "04": "AZ",
    "05": "AR",
    "06": "CA",
    "08": "CO",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "15": "HI",
    "16": "ID",
    "17": "IL",
    "18": "IN",
    "19": "IA",
    "20": "KS",
    "21": "KY",
    "22": "LA",
    "23": "ME",
    "24": "MD",
    "25": "MA",
    "26": "MI",
    "27": "MN",
    "28": "MS",
    "29": "MO",
    "30": "MT",
    "31": "NE",
    "32": "NV",
    "33": "NH",
    "34": "NJ",
    "35": "NM",
    "36": "NY",
    "37": "NC",
    "38": "ND",
    "39": "OH",
    "40": "OK",
    "41": "OR",
    "42": "PA",
    "44": "RI",
    "45": "SC",
    "46": "SD",
    "47": "TN",
    "48": "TX",
    "49": "UT",
    "50": "VT",
    "51": "VA",
    "53": "WA",
    "54": "WV",
    "55": "WI",
    "56": "WY",
    "60": "AS",
    "66": "GU",
    "69": "MP",
    "72": "PR",
    "78": "VI",
}

COUNTY_SUFFIXES = (
    "county",
    "parish",
    "borough",
    "census area",
    "municipio",
    "municipality",
    "city and borough",
    "city",
)


def load_zip_county_mapping(
    file_path: str | Path,
    database_path: str | Path | None = None,
    source: str | None = None,
) -> int:
    init_db(database_path)
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"ZIP-county mapping file not found: {path}")

    raw = read_zip_county_file(path)
    normalized = normalize_zip_county_mapping(raw, source=source or str(path))
    with get_connection(database_path) as con:
        con.execute("DELETE FROM zip_county_mapping")
        _insert_df(con, "zip_county_mapping", normalized)
    LOGGER.info("Loaded %s ZIP-county mapping rows from %s", len(normalized), path)
    return len(normalized)


def read_zip_county_file(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    with path.open("r", encoding="utf-8-sig") as handle:
        header = handle.readline()
    if "|" in header:
        return pd.read_csv(path, dtype=str, sep="|", encoding="utf-8-sig")
    if "\t" in header:
        return pd.read_csv(path, dtype=str, sep="\t", encoding="utf-8-sig")
    return pd.read_csv(path, dtype=str, sep=None, engine="python", encoding="utf-8-sig")


def normalize_zip_county_mapping(df: pd.DataFrame, source: str) -> pd.DataFrame:
    zip_col = _first_existing(df, ["zip code", "zip", "zipcode", "zcta", "ZCTA5", "GEOID_ZCTA5_20"])
    county_fips_col = _first_existing(df, ["county fips", "county", "county_fips", "COUNTY", "GEOID_COUNTY_20"])
    county_name_col = _first_existing(
        df,
        ["county name", "county_name", "COUNTYNAME", "NAMELSAD_COUNTY_20", "AREANAME_COUNTY_20"],
    )
    state_code_col = _first_existing(df, ["state code", "state", "state_code", "USPS"])
    state_name_col = _first_existing(df, ["state name", "state_name"])
    weight_col = _first_existing(
        df,
        [
            "allocation_weight",
            "res_ratio",
            "RES_RATIO",
            "tot_ratio",
            "TOT_RATIO",
            "bus_ratio",
            "BUS_RATIO",
        ],
    )
    area_land_part_col = _first_existing(df, ["AREALAND_PART", "area_land_part"])
    area_water_part_col = _first_existing(df, ["AREAWATER_PART", "area_water_part"])
    if zip_col is None or county_fips_col is None:
        raise ValueError("ZIP-county file needs ZIP/ZCTA and county FIPS columns")

    output = pd.DataFrame()
    output["zcta"] = df[zip_col].map(clean_zip)
    output["county_fips"] = df[county_fips_col].map(_clean_county_fips)
    output["county_name"] = df[county_name_col].map(_clean_label) if county_name_col else ""
    output["state_code"] = df[state_code_col].map(_clean_state_code) if state_code_col else ""
    output["state_name"] = df[state_name_col].map(_clean_label) if state_name_col else ""
    output["allocation_weight"] = _allocation_weights(
        df,
        explicit_weight_col=weight_col,
        area_land_part_col=area_land_part_col,
        area_water_part_col=area_water_part_col,
    )
    output["source"] = source
    output["loaded_at"] = utc_now_naive()

    output = output.dropna(subset=["zcta", "county_fips"]).copy()
    output["state_code"] = output.apply(_state_code_from_row, axis=1)
    output = _dedupe_and_normalize_weights(output)
    return output[_columns()].where(pd.notna(output[_columns()]), None)


def _allocation_weights(
    df: pd.DataFrame,
    *,
    explicit_weight_col: str | None,
    area_land_part_col: str | None,
    area_water_part_col: str | None,
) -> pd.Series:
    if explicit_weight_col:
        return pd.to_numeric(df[explicit_weight_col], errors="coerce")

    land = pd.to_numeric(df[area_land_part_col], errors="coerce") if area_land_part_col else None
    water = pd.to_numeric(df[area_water_part_col], errors="coerce") if area_water_part_col else None
    if land is not None and (land.fillna(0) > 0).any():
        return land
    if land is not None and water is not None:
        return land.fillna(0) + water.fillna(0)
    if water is not None and (water.fillna(0) > 0).any():
        return water
    return pd.Series(pd.NA, index=df.index)


def county_lookup_from_mapping(mapping: pd.DataFrame) -> dict[tuple[str, str], tuple[str, str]]:
    lookup: dict[tuple[str, str], tuple[str, str]] = {}
    for row in mapping.dropna(subset=["county_fips"]).itertuples(index=False):
        state_code = str(getattr(row, "state_code") or "")
        county_name = str(getattr(row, "county_name") or "")
        county_fips = str(getattr(row, "county_fips") or "")
        if state_code and county_name and county_fips:
            lookup[(state_code, normalize_county_key(county_name))] = (county_fips, county_name)
    return lookup


def normalize_county_key(value: object | None) -> str:
    text = normalize_for_match(value)
    text = re.sub(r"\b(st)\b", "saint", text)
    for suffix in sorted(COUNTY_SUFFIXES, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(suffix)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_and_normalize_weights(output: pd.DataFrame) -> pd.DataFrame:
    group_cols = ["zcta", "county_fips", "county_name", "state_code", "state_name", "source", "loaded_at"]
    output = (
        output.groupby(group_cols, dropna=False, as_index=False)
        .agg(allocation_weight=("allocation_weight", "sum"))
        .copy()
    )
    output["allocation_weight"] = pd.to_numeric(output["allocation_weight"], errors="coerce").astype(float)
    missing_weight = output["allocation_weight"].isna() | (output["allocation_weight"].astype(float) <= 0)
    if missing_weight.any():
        counts = output.groupby("zcta")["county_fips"].transform("count")
        output.loc[missing_weight, "allocation_weight"] = 1.0 / counts.loc[missing_weight].astype(float)

    totals = output.groupby("zcta")["allocation_weight"].transform("sum")
    output["allocation_weight"] = output["allocation_weight"].astype(float) / totals.astype(float)
    return output.sort_values(["zcta", "county_fips"]).reset_index(drop=True)


def _clean_county_fips(value: object | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    return digits.zfill(5)[-5:]


def _clean_label(value: object | None) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else re.sub(r"\s+", " ", text)


def _clean_state_code(value: object | None) -> str:
    text = _clean_label(value).upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) else ""


def _state_code_from_row(row: pd.Series) -> str:
    state_code = str(row.get("state_code") or "")
    if state_code:
        return state_code
    county_fips = str(row.get("county_fips") or "")
    return STATE_FIPS_TO_CODE.get(county_fips[:2], "")


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    normalized = {_normalize_column(column): column for column in df.columns}
    for name in names:
        if name in df.columns:
            return name
        key = _normalize_column(name)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_column(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _columns() -> list[str]:
    return [
        "zcta",
        "county_fips",
        "county_name",
        "state_code",
        "state_name",
        "allocation_weight",
        "source",
        "loaded_at",
    ]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
