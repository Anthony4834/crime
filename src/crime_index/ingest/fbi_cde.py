from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.db import get_connection, init_db
from crime_index.ingest.zip_county_loader import county_lookup_from_mapping, normalize_county_key
from crime_index.utils.text_utils import normalize_for_match
from crime_index.utils.time_utils import utc_now_naive

LOGGER = logging.getLogger(__name__)

PUBLIC_CDE_BROWSER_API_KEY = "iiHnOKfno2Mgkt5AynpvPpUQTEyxE77jo1RU8PIv"
AGENCY_API_BASE = "https://api.usa.gov/crime/fbi/cde"
CIUS_CATALOG_URL = "https://cde.ucr.cjis.gov/LATEST/webapp/assets/JSON/downloads/cius.json"
CIUS_SIGNED_URL = "https://cde.ucr.cjis.gov/LATEST/s3/signedurl"
CIUS_COLLECTION_ID = "offenses-known-to-le"
COUNTY_SCOPE_SOURCE = "fbi_cde_cius_offenses_known"

STATE_CODE_TO_NAME = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "AS": "American Samoa",
    "GU": "Guam",
    "GM": "Guam",
    "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands",
}
STATE_NAME_TO_CODE = {normalize_for_match(name): code for code, name in STATE_CODE_TO_NAME.items()}

DEFAULT_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "PR",
]

TABLE_MARKERS = {
    "8": "CIUS_Table_8_",
    "9": "CIUS_Table_9_",
    "10": "CIUS_Table_10_",
    "11": "CIUS_Table_11_",
}

COUNT_COLUMNS = [
    "violent_crime_count",
    "property_crime_count",
    "murder_count",
    "rape_count",
    "robbery_count",
    "aggravated_assault_count",
    "burglary_count",
    "larceny_theft_count",
    "motor_vehicle_theft_count",
    "arson_count",
]


def download_cius_offenses_known(
    year: int,
    output_dir: str | Path = "data/raw/fbi_cde",
    force: bool = False,
) -> Path:
    destination = Path(output_dir) / str(year) / f"offenses-known-to-le-{year}.zip"
    if destination.exists() and not force:
        return destination

    catalog = _fetch_json(CIUS_CATALOG_URL)
    download_file = _cius_download_filename(catalog, year)
    key = f"cius/{year}/{download_file}"
    signed = _fetch_json(f"{CIUS_SIGNED_URL}?{urlencode({'key': key})}")
    url = signed.get(key)
    if not url:
        raise RuntimeError(f"CDE signed URL response did not include {key}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "crime-index-local-pipeline/0.1"})
    with urlopen(request, timeout=300) as response:
        destination.write_bytes(response.read())
    LOGGER.info("Downloaded CDE CIUS offenses package to %s", destination)
    return destination


def load_fbi_cde_agencies(
    states: list[str] | None = None,
    database_path: str | Path | None = None,
    force: bool = False,
    cache_dir: str | Path = "data/raw/fbi_cde/agencies",
    settings: dict[str, Any] | None = None,
) -> int:
    init_db(database_path)
    settings = settings or load_settings()
    states = [state.upper() for state in (states or DEFAULT_STATES)]
    api_key = _api_key(settings)

    with get_connection(database_path) as con:
        mapping = con.execute("SELECT * FROM zip_county_mapping").fetchdf()
        county_lookup = county_lookup_from_mapping(mapping)

    rows: list[dict[str, Any]] = []
    for state in states:
        payload = fetch_agencies_by_state(state, api_key=api_key, cache_dir=cache_dir, force=force)
        rows.extend(_agency_rows_from_payload(payload, state, county_lookup))
        time.sleep(float(settings.get("fbi_cde", {}).get("request_delay_seconds", 0.05)))

    df = pd.DataFrame(rows, columns=_agency_columns())
    with get_connection(database_path) as con:
        if states:
            placeholders = ", ".join(["?"] * len(states))
            con.execute(f"DELETE FROM fbi_cde_agencies WHERE state_code IN ({placeholders})", states)
        _insert_df(con, "fbi_cde_agencies", df)
    LOGGER.info("Loaded %s FBI CDE agency lookup rows", len(df))
    return len(df)


def fetch_agencies_by_state(
    state_code: str,
    api_key: str,
    cache_dir: str | Path = "data/raw/fbi_cde/agencies",
    force: bool = False,
) -> dict[str, Any]:
    cache_path = Path(cache_dir) / f"{state_code.upper()}.json"
    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    params = urlencode({"API_KEY": api_key})
    url = f"{AGENCY_API_BASE}/agency/byStateAbbr/{state_code.upper()}?{params}"
    payload = _fetch_json(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return payload


def load_cius_offenses_known(
    year: int,
    zip_path: str | Path | None = None,
    database_path: str | Path | None = None,
    table_numbers: list[str] | None = None,
) -> int:
    init_db(database_path)
    zip_path = Path(zip_path) if zip_path else download_cius_offenses_known(year)
    table_numbers = table_numbers or ["8", "9", "10", "11"]

    with get_connection(database_path) as con:
        mapping = con.execute("SELECT * FROM zip_county_mapping").fetchdf()
        agencies = con.execute("SELECT * FROM fbi_cde_agencies").fetchdf()
    county_lookup = county_lookup_from_mapping(mapping)
    city_lookup = _city_county_lookup(agencies)
    agency_lookup = _agency_county_lookup(agencies)

    rows = parse_cius_offenses_known_zip(
        zip_path,
        year=year,
        table_numbers=table_numbers,
        county_lookup=county_lookup,
        city_lookup=city_lookup,
        agency_lookup=agency_lookup,
    )
    with get_connection(database_path) as con:
        con.execute("DELETE FROM fbi_cde_cius_agency_offenses WHERE year = ?", [year])
        _insert_df(con, "fbi_cde_cius_agency_offenses", rows)
    LOGGER.info("Loaded %s CDE CIUS offense rows for %s", len(rows), year)
    return len(rows)


def parse_cius_offenses_known_zip(
    zip_path: str | Path,
    year: int,
    table_numbers: list[str],
    county_lookup: dict[tuple[str, str], tuple[str, str]],
    city_lookup: dict[tuple[str, str], tuple[str, str, int]],
    agency_lookup: dict[tuple[str, str], tuple[str, str, int]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path) as archive:
        for table_number in table_numbers:
            marker = TABLE_MARKERS[table_number]
            workbook_names = [
                name
                for name in archive.namelist()
                if marker in name and name.lower().endswith(".xlsx") and not name.endswith("/")
            ]
            if not workbook_names:
                LOGGER.warning("No CIUS table %s workbook found in %s", table_number, zip_path)
                continue
            rows.extend(
                _parse_cius_table(
                    archive.read(workbook_names[0]),
                    table_number,
                    year,
                    workbook_names[0],
                    county_lookup,
                    city_lookup,
                    agency_lookup,
                )
            )
    df = pd.DataFrame(rows, columns=_cius_columns())
    for column in ["population_reported", *COUNT_COLUMNS]:
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    return df.where(pd.notna(df), None)


def _parse_cius_table(
    workbook_bytes: bytes,
    table_number: str,
    year: int,
    source_file: str,
    county_lookup: dict[tuple[str, str], tuple[str, str]],
    city_lookup: dict[tuple[str, str], tuple[str, str, int]],
    agency_lookup: dict[tuple[str, str], tuple[str, str, int]],
) -> list[dict[str, Any]]:
    raw = pd.read_excel(io.BytesIO(workbook_bytes), sheet_name=0, header=None, dtype=object)
    header_idx = _find_header_row(raw)
    headers = [_normalize_cius_column(value, index) for index, value in enumerate(raw.iloc[header_idx].tolist())]
    table = raw.iloc[header_idx + 1 :].copy()
    table.columns = headers
    table = table.dropna(how="all")

    rows: list[dict[str, Any]] = []
    loaded_at = utc_now_naive()
    for record in table.to_dict(orient="records"):
        state_name = _clean_text(record.get("state"))
        state_code = STATE_NAME_TO_CODE.get(normalize_for_match(state_name))
        if not state_code:
            continue
        agency_label, agency_type, population = _agency_fields(record, table_number)
        if not agency_label:
            continue
        county_fips, county_name, mapping_method, mapping_notes = _resolve_county(
            record,
            table_number,
            state_code,
            agency_label,
            county_lookup,
            city_lookup,
            agency_lookup,
        )
        rows.append(
            {
                "year": year,
                "table_number": table_number,
                "table_name": f"CIUS Table {table_number}",
                "state_code": state_code,
                "state_name": STATE_CODE_TO_NAME.get(state_code, state_name or state_code),
                "agency_label": agency_label,
                "agency_type": agency_type,
                "county_fips": county_fips,
                "county_name": county_name,
                "population_reported": population,
                "violent_crime_count": _to_int(record.get("violent_crime")),
                "property_crime_count": _to_int(record.get("property_crime")),
                "murder_count": _to_int(record.get("murder_and_nonnegligent_manslaughter")),
                "rape_count": _to_int(record.get("rape")),
                "robbery_count": _to_int(record.get("robbery")),
                "aggravated_assault_count": _to_int(record.get("aggravated_assault")),
                "burglary_count": _to_int(record.get("burglary")),
                "larceny_theft_count": _to_int(record.get("larceny_theft")),
                "motor_vehicle_theft_count": _to_int(record.get("motor_vehicle_theft")),
                "arson_count": _to_int(record.get("arson")),
                "mapping_method": mapping_method,
                "mapping_notes": mapping_notes,
                "source_file": source_file,
                "loaded_at": loaded_at,
            }
        )
    return rows


def _resolve_county(
    record: dict[str, Any],
    table_number: str,
    state_code: str,
    agency_label: str,
    county_lookup: dict[tuple[str, str], tuple[str, str]],
    city_lookup: dict[tuple[str, str], tuple[str, str, int]],
    agency_lookup: dict[tuple[str, str], tuple[str, str, int]],
) -> tuple[str | None, str | None, str, str | None]:
    if table_number == "10":
        key = (state_code, normalize_county_key(record.get("county")))
        resolved = county_lookup.get(key)
        if resolved:
            return resolved[0], resolved[1], "county_name", None
        return None, None, "unmatched_county_name", f"county_not_found:{record.get('county')}"

    if table_number == "8":
        key = (state_code, normalize_place_key(agency_label))
        resolved = city_lookup.get(key)
        if resolved:
            county_fips, county_name, match_count = resolved
            method = "agency_city_lookup" if match_count == 1 else "agency_city_lookup_ambiguous_same_county"
            return county_fips, county_name, method, None
        return None, None, "unmatched_city", f"city_not_found:{agency_label}"

    key = (state_code, normalize_agency_key(agency_label))
    resolved = agency_lookup.get(key)
    if resolved:
        county_fips, county_name, match_count = resolved
        method = "agency_name_lookup" if match_count == 1 else "agency_name_lookup_ambiguous_same_county"
        return county_fips, county_name, method, None
    return None, None, "unmatched_agency_name", f"agency_not_found:{agency_label}"


def _agency_fields(record: dict[str, Any], table_number: str) -> tuple[str | None, str, int | None]:
    if table_number == "8":
        return _clean_text(record.get("city")), "city", _to_int(record.get("population"))
    if table_number == "9":
        return _clean_text(record.get("university_college")), "university_college", _to_int(
            record.get("student_enrollment")
        )
    if table_number == "10":
        return _clean_text(record.get("county")), "county", None
    if table_number == "11":
        agency = _clean_text(record.get("agency"))
        unit = _clean_text(record.get("unit_office"))
        label = f"{agency} - {unit}" if agency and unit else agency
        agency_type = _clean_text(record.get("state_tribal_other")) or "state_tribal_other"
        return label, agency_type, None
    raise ValueError(f"Unsupported CIUS table: {table_number}")


def _agency_rows_from_payload(
    payload: dict[str, Any],
    state_code: str,
    county_lookup: dict[tuple[str, str], tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    loaded_at = utc_now_naive()
    for county_bucket, agencies in payload.items():
        if not isinstance(agencies, list):
            continue
        for agency in agencies:
            if not isinstance(agency, dict):
                continue
            county_fips, county_name = _resolve_agency_counties(
                str(agency.get("counties") or county_bucket),
                state_code,
                county_lookup,
            )
            rows.append(
                {
                    "ori": agency.get("ori"),
                    "state_code": agency.get("state_abbr") or state_code,
                    "state_name": agency.get("state_name") or STATE_CODE_TO_NAME.get(state_code),
                    "agency_name": agency.get("agency_name"),
                    "agency_type_name": agency.get("agency_type_name"),
                    "counties": agency.get("counties") or county_bucket,
                    "county_fips": county_fips,
                    "county_name": county_name,
                    "is_nibrs": agency.get("is_nibrs"),
                    "latitude": _to_float(agency.get("latitude")),
                    "longitude": _to_float(agency.get("longitude")),
                    "nibrs_start_date": agency.get("nibrs_start_date"),
                    "source_json": json.dumps(agency, sort_keys=True),
                    "loaded_at": loaded_at,
                }
            )
    return rows


def _resolve_agency_counties(
    counties: str,
    state_code: str,
    county_lookup: dict[tuple[str, str], tuple[str, str]],
) -> tuple[str | None, str | None]:
    names = [name for name in re.split(r"[,;/]| and ", counties or "") if name.strip()]
    resolved = [county_lookup.get((state_code, normalize_county_key(name))) for name in names]
    resolved = [item for item in resolved if item]
    if not resolved:
        return None, None
    unique = sorted(set(resolved))
    return "|".join(item[0] for item in unique), "|".join(item[1] for item in unique)


def _city_county_lookup(agencies: pd.DataFrame) -> dict[tuple[str, str], tuple[str, str, int]]:
    candidates: dict[tuple[str, str], list[tuple[str, str]]] = {}
    if agencies.empty:
        return {}
    for row in agencies.dropna(subset=["agency_name", "county_fips"]).itertuples(index=False):
        county_fips = str(getattr(row, "county_fips") or "")
        county_name = str(getattr(row, "county_name") or "")
        if "|" in county_fips or not county_fips:
            continue
        state_code = str(getattr(row, "state_code") or "")
        key = (state_code, city_key_from_agency_name(getattr(row, "agency_name")))
        if key[1]:
            candidates.setdefault(key, []).append((county_fips, county_name))
    return _unique_same_county_lookup(candidates)


def _agency_county_lookup(agencies: pd.DataFrame) -> dict[tuple[str, str], tuple[str, str, int]]:
    candidates: dict[tuple[str, str], list[tuple[str, str]]] = {}
    if agencies.empty:
        return {}
    for row in agencies.dropna(subset=["agency_name", "county_fips"]).itertuples(index=False):
        county_fips = str(getattr(row, "county_fips") or "")
        county_name = str(getattr(row, "county_name") or "")
        if "|" in county_fips or not county_fips:
            continue
        state_code = str(getattr(row, "state_code") or "")
        keys = {
            normalize_agency_key(getattr(row, "agency_name")),
            city_key_from_agency_name(getattr(row, "agency_name")),
        }
        for key_value in keys:
            if key_value:
                candidates.setdefault((state_code, key_value), []).append((county_fips, county_name))
    return _unique_same_county_lookup(candidates)


def _unique_same_county_lookup(
    candidates: dict[tuple[str, str], list[tuple[str, str]]],
) -> dict[tuple[str, str], tuple[str, str, int]]:
    lookup: dict[tuple[str, str], tuple[str, str, int]] = {}
    for key, values in candidates.items():
        unique = sorted(set(values))
        if len({value[0] for value in unique}) == 1:
            lookup[key] = (unique[0][0], unique[0][1], len(values))
    return lookup


def normalize_place_key(value: object | None) -> str:
    text = normalize_for_match(value)
    text = re.sub(r"\b(city|town|village|borough)\b$", " ", text)
    text = re.sub(r"\b\d+\b$", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_agency_key(value: object | None) -> str:
    text = normalize_for_match(value)
    replacements = [
        r"\bpolice department\b",
        r"\bdepartment of police\b",
        r"\bdept of police\b",
        r"\bpublic safety department\b",
        r"\bdepartment of public safety\b",
        r"\bdept of public safety\b",
        r"\bsheriff s office\b",
        r"\bsheriff office\b",
        r"\bsheriff department\b",
        r"\bdepartment\b",
        r"\bpolice\b",
    ]
    for pattern in replacements:
        text = re.sub(pattern, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def city_key_from_agency_name(value: object | None) -> str:
    return normalize_place_key(normalize_agency_key(value))


def _find_header_row(raw: pd.DataFrame) -> int:
    for idx, row in raw.iterrows():
        values = [normalize_for_match(value) for value in row.tolist()]
        if "state" in values and any(value == "violent crime" for value in values):
            return int(idx)
    raise ValueError("Could not find CIUS table header row")


def _normalize_cius_column(value: object, index: int) -> str:
    text = normalize_for_match(value)
    if not text:
        return f"unnamed_{index}"
    replacements = {
        "violent crime": "violent_crime",
        "property crime": "property_crime",
        "murder and nonnegligent manslaughter": "murder_and_nonnegligent_manslaughter",
        "aggravated assault": "aggravated_assault",
        "larceny theft": "larceny_theft",
        "motor vehicle theft": "motor_vehicle_theft",
        "student enrollment1": "student_enrollment",
        "student enrollment": "student_enrollment",
        "university college": "university_college",
        "state tribal other": "state_tribal_other",
        "unit office": "unit_office",
    }
    if text in replacements:
        return replacements[text]
    text = re.sub(r"\b1\b$", "", text)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _clean_text(value: object | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"(?<=\D)\d+$", "", text).strip()


def _to_int(value: object | None) -> int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if not cleaned or cleaned in {"-", "--"}:
            return None
    else:
        cleaned = value
    try:
        return int(float(cleaned))
    except (TypeError, ValueError):
        return None


def _to_float(value: object | None) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cius_download_filename(catalog: dict[str, Any], year: int) -> str:
    year_key = str(year)
    for collection in catalog.get("collections", []):
        if collection.get("id") == CIUS_COLLECTION_ID:
            download = (collection.get("downloads") or {}).get(year_key)
            if download:
                return str(download)
    raise ValueError(f"No {CIUS_COLLECTION_ID} download found for {year}")


def _api_key(settings: dict[str, Any]) -> str:
    configured = settings.get("fbi_cde", {})
    return (
        os.getenv(str(configured.get("api_key_env", "FBI_CDE_API_KEY")))
        or configured.get("api_key")
        or PUBLIC_CDE_BROWSER_API_KEY
    )


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "crime-index-local-pipeline/0.1"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _agency_columns() -> list[str]:
    return [
        "ori",
        "state_code",
        "state_name",
        "agency_name",
        "agency_type_name",
        "counties",
        "county_fips",
        "county_name",
        "is_nibrs",
        "latitude",
        "longitude",
        "nibrs_start_date",
        "source_json",
        "loaded_at",
    ]


def _cius_columns() -> list[str]:
    return [
        "year",
        "table_number",
        "table_name",
        "state_code",
        "state_name",
        "agency_label",
        "agency_type",
        "county_fips",
        "county_name",
        "population_reported",
        *COUNT_COLUMNS,
        "mapping_method",
        "mapping_notes",
        "source_file",
        "loaded_at",
    ]


def _insert_df(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("_insert_df", df)
    columns = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM _insert_df")
    con.unregister("_insert_df")
