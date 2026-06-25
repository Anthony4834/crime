from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)

STATE_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
    "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
    "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
    "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
    "56", "60", "66", "69", "72", "78",
]


def fetch_census_reporter_zcta_population(
    output_file: str | Path,
    release: str = "latest",
    state_fips: list[str] | None = None,
) -> dict[str, object]:
    """Fetch ACS B01003 population for all ZCTAs by state from Census Reporter."""

    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    rows_by_zcta: dict[str, dict[str, object]] = {}
    releases: dict[str, str] = {}
    failures: dict[str, str] = {}

    for fips in state_fips or STATE_FIPS:
        geo_id = f"860|04000US{fips}"
        url = (
            f"https://api.censusreporter.org/1.0/data/show/{quote(release)}"
            f"?table_ids=B01003&geo_ids={quote(geo_id, safe='|')}"
        )
        request = Request(url, headers={"User-Agent": "crime-index-local-pipeline/0.1"})
        try:
            with urlopen(request, timeout=120) as response:
                payload = json.load(response)
        except Exception as exc:
            LOGGER.warning("Failed Census Reporter population fetch for state FIPS %s: %s", fips, exc)
            failures[fips] = str(exc)
            continue

        release_meta = payload.get("release", {})
        release_name = release_meta.get("name", "")
        release_years = release_meta.get("years", "")
        if release_name:
            releases[str(release_meta.get("id", release_name))] = f"{release_name} {release_years}".strip()
        for geo_id_key, tables in payload.get("data", {}).items():
            geography = payload.get("geography", {}).get(geo_id_key, {})
            name = str(geography.get("name") or geo_id_key[-5:])
            digits = "".join(ch for ch in name if ch.isdigit())
            zcta = digits[-5:] if len(digits) >= 5 else geo_id_key[-5:]
            estimate = tables.get("B01003", {}).get("estimate", {}).get("B01003001")
            error = tables.get("B01003", {}).get("error", {}).get("B01003001")
            if len(zcta) != 5 or estimate is None:
                continue
            rows_by_zcta[zcta] = {
                "ZCTA": zcta,
                "NAME": f"ZCTA5 {zcta}",
                "B01003_001E": int(estimate),
                "B01003_001M": int(error) if error is not None else "",
                "source_release": release_name,
                "source_years": release_years,
            }
        LOGGER.info("Fetched %s ZCTA population rows after state FIPS %s", len(rows_by_zcta), fips)

    rows = [rows_by_zcta[zcta] for zcta in sorted(rows_by_zcta)]
    with output_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ZCTA", "NAME", "B01003_001E", "B01003_001M", "source_release", "source_years"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return {
        "rows": len(rows),
        "output_file": str(output_file),
        "releases": releases,
        "failures": failures,
    }
