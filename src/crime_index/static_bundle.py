from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from crime_index.utils.time_utils import utc_now_naive

SCORES_FILE_PATTERN = re.compile(r"^zcta_crime_scores_(?P<year>\d{4})(?:_(?P<scope>.+))?\.csv$")
COVERAGE_FILE_PATTERN = re.compile(r"^zcta_national_coverage_(?P<year>\d{4})\.csv$")
COMBINED_SCOPE = "national_combined"
OBSERVED_SCOPE = "source_universe"
MODELED_SCOPE = "national_modeled_baseline"

INT_FIELDS = {
    "year",
    "population_total",
    "source_count",
    "assigned_incident_count",
    "spatial_incident_count",
    "total_crime_count",
    "violent_crime_count",
    "property_crime_count",
    "drug_crime_count",
    "public_order_crime_count",
    "weapons_crime_count",
    "other_crime_count",
    "unknown_crime_count",
}
BOOL_FIELDS = {"is_modeled", "is_modelled"}


def build_static_bundle(
    export_dir: str | Path = "data/exports",
    output_dir: str | Path = "data/server",
    years: list[int] | None = None,
    scopes: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> dict[str, Any]:
    export_dir = Path(export_dir)
    output_dir = Path(output_dir)
    year_filter = {str(year) for year in years} if years else None
    scope_filter = set(scopes) if scopes else None
    _clear_generated_api_files(output_dir)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_type": "yearly_static_files",
        "generated_at": utc_now_naive().isoformat(),
        "cors": {
            "hosting": "github_pages",
            "note": "GitHub Pages serves static assets with permissive CORS. These origins are the intended consumers, not an enforceable allowlist.",
            "intended_consumer_origins": allowed_origins or [],
        },
        "years": {},
    }
    score_records: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for csv_path in sorted(export_dir.glob("zcta_crime_scores_*.csv")):
        match = SCORES_FILE_PATTERN.match(csv_path.name)
        if not match:
            continue
        year = match.group("year")
        scope = match.group("scope") or "source_universe"
        if year_filter and year not in year_filter:
            continue
        if scope_filter and scope not in scope_filter:
            continue

        records = _read_csv_records(csv_path)
        score_records[(year, scope)] = records
        relative_path = Path(year) / scope / "scores.json"
        output_path = output_dir / relative_path
        _write_json(
            output_path,
            {
                "year": int(year),
                "scope": scope,
                "row_count": len(records),
                "records": records,
            },
        )

        year_entry = manifest["years"].setdefault(year, {"scopes": {}})
        year_entry["scopes"][scope] = {
            "path": relative_path.as_posix(),
            "row_count": len(records),
            "sha256": _sha256(output_path),
            "coverage_status_counts": _counts(records, "coverage_status"),
            "data_source_type_counts": _counts(records, "data_source_type"),
        }

    _write_combined_scopes(output_dir, manifest, score_records, scope_filter)

    for csv_path in sorted(export_dir.glob("zcta_national_coverage_*.csv")):
        match = COVERAGE_FILE_PATTERN.match(csv_path.name)
        if not match:
            continue
        year = match.group("year")
        if year_filter and year not in year_filter:
            continue
        records = _read_csv_records(csv_path)
        relative_path = Path(year) / "coverage.json"
        output_path = output_dir / relative_path
        _write_json(
            output_path,
            {
                "year": int(year),
                "row_count": len(records),
                "records": records,
            },
        )
        year_entry = manifest["years"].setdefault(year, {"scopes": {}})
        year_entry["coverage"] = {
            "path": relative_path.as_posix(),
            "row_count": len(records),
            "sha256": _sha256(output_path),
            "coverage_status_counts": _counts(records, "coverage_status"),
            "data_source_type_counts": _counts(records, "data_source_type"),
        }

    for year_entry in manifest["years"].values():
        scopes_for_year = year_entry.get("scopes", {})
        if COMBINED_SCOPE in scopes_for_year:
            year_entry["default_scope"] = COMBINED_SCOPE
        elif MODELED_SCOPE in scopes_for_year:
            year_entry["default_scope"] = MODELED_SCOPE
        elif OBSERVED_SCOPE in scopes_for_year:
            year_entry["default_scope"] = OBSERVED_SCOPE
        elif scopes_for_year:
            year_entry["default_scope"] = sorted(scopes_for_year)[0]

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    (output_dir / "crime-data-client.js").write_text(_client_js(), encoding="utf-8")
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def _write_combined_scopes(
    output_dir: Path,
    manifest: dict[str, Any],
    score_records: dict[tuple[str, str], list[dict[str, Any]]],
    scope_filter: set[str] | None,
) -> None:
    if scope_filter and COMBINED_SCOPE not in scope_filter:
        return
    years = sorted({year for year, _ in score_records})
    for year in years:
        observed = score_records.get((year, OBSERVED_SCOPE))
        modeled = score_records.get((year, MODELED_SCOPE))
        if not observed or not modeled:
            continue

        by_zcta = {str(record["zcta"]): dict(record) for record in modeled}
        for record in observed:
            by_zcta[str(record["zcta"])] = dict(record)

        records = [by_zcta[zcta] for zcta in sorted(by_zcta)]
        for record in records:
            record["comparison_scope"] = COMBINED_SCOPE
            record["comparison_scope_value"] = ""

        relative_path = Path(year) / COMBINED_SCOPE / "scores.json"
        output_path = output_dir / relative_path
        _write_json(
            output_path,
            {
                "year": int(year),
                "scope": COMBINED_SCOPE,
                "row_count": len(records),
                "records": records,
            },
        )

        year_entry = manifest["years"].setdefault(year, {"scopes": {}})
        year_entry["scopes"][COMBINED_SCOPE] = {
            "path": relative_path.as_posix(),
            "row_count": len(records),
            "sha256": _sha256(output_path),
            "coverage_status_counts": _counts(records, "coverage_status"),
            "data_source_type_counts": _counts(records, "data_source_type"),
        }
        year_entry["zip_api"] = _write_zip_api(output_dir, year, records)


def _write_zip_api(output_dir: Path, year: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    base_path = Path("api") / "v1" / year / "zips"
    for record in records:
        zcta = str(record["zcta"]).zfill(5)
        api_record = dict(record)
        api_record["zip"] = zcta
        api_record["zcta"] = zcta
        api_record["api_version"] = "v1"
        api_record["api_path"] = f"/{base_path.as_posix()}/{zcta}.json"
        _write_json(output_dir / base_path / f"{zcta}.json", api_record)
    return {
        "scope": COMBINED_SCOPE,
        "path_template": f"{base_path.as_posix()}/{{zip}}.json",
        "row_count": len(records),
    }


def check_static_cors(base_url: str, origins: list[str]) -> list[dict[str, Any]]:
    manifest_url = base_url.rstrip("/") + "/manifest.json"
    results: list[dict[str, Any]] = []
    for origin in origins:
        request = Request(manifest_url, headers={"Origin": origin})
        try:
            with urlopen(request, timeout=20) as response:
                allow_origin = response.headers.get("Access-Control-Allow-Origin")
                results.append(
                    {
                        "origin": origin,
                        "url": manifest_url,
                        "access_control_allow_origin": allow_origin,
                        "allowed": allow_origin == "*" or allow_origin == origin,
                        "status": response.status,
                    }
                )
        except Exception as exc:
            results.append(
                {
                    "origin": origin,
                    "url": manifest_url,
                    "access_control_allow_origin": None,
                    "allowed": False,
                    "status": None,
                    "error": str(exc),
                }
            )
    return results


def _clear_generated_api_files(output_dir: Path) -> None:
    api_dir = output_dir / "api" / "v1"
    if api_dir.exists():
        shutil.rmtree(api_dir)


def _read_csv_records(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [_coerce_record(row) for row in reader]


def _coerce_record(row: dict[str, str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in row.items():
        record[key] = _coerce_value(key, value)
    return record


def _coerce_value(key: str, value: str | None) -> Any:
    if value is None:
        return None
    stripped = value.strip()
    if stripped == "":
        return "" if key in {"zcta", "comparison_scope", "comparison_scope_value", "source_names"} else None
    if key == "zcta":
        return stripped.zfill(5) if stripped.isdigit() else stripped
    if key in BOOL_FIELDS:
        return stripped.lower() in {"true", "1", "yes"}
    if key in INT_FIELDS:
        return int(float(stripped))
    if _looks_numeric_field(key):
        return float(stripped)
    return stripped


def _looks_numeric_field(key: str) -> bool:
    return (
        key.endswith("_rate_per_1000")
        or key.endswith("_winsorized_per_1000")
        or key.endswith("_score_0_100")
        or key.endswith("_percentile")
        or key.endswith("_index")
        or key.endswith("_z_score")
        or key in {"data_coverage_score", "percentile_rank", "composite_index"}
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(key)
        label = "null" if value is None else str(value)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _client_js() -> str:
    return """export async function loadCrimeData(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");

  const manifest = await getJson(`${baseUrl}/manifest.json`);
  const year = String(options.year || latestYear(manifest));
  const yearInfo = manifest.years?.[year];
  if (!yearInfo) throw new Error(`Year ${year} is not available`);

  const scope = options.scope || yearInfo.default_scope;
  const scopeInfo = yearInfo.scopes?.[scope];
  if (!scopeInfo) throw new Error(`Scope ${scope} is not available for ${year}`);

  const scores = await getJson(`${baseUrl}/${scopeInfo.path}`);
  const byZcta = new Map(scores.records.map((record) => [normalizeZcta(record.zcta), record]));

  return {
    manifest,
    year: Number(year),
    scope,
    rowCount: scores.row_count,
    records: scores.records,
    getZcta(zcta) {
      return byZcta.get(normalizeZcta(zcta)) || null;
    },
    getMany(zctas) {
      return zctas.map((zcta) => byZcta.get(normalizeZcta(zcta)) || null);
    }
  };
}

export async function loadCoverage(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");

  const manifest = await getJson(`${baseUrl}/manifest.json`);
  const year = String(options.year || latestYear(manifest));
  const coverageInfo = manifest.years?.[year]?.coverage;
  if (!coverageInfo) throw new Error(`Coverage is not available for ${year}`);
  return getJson(`${baseUrl}/${coverageInfo.path}`);
}

export async function getCrimeStatsForZip(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");
  if (!options.zip) throw new Error("zip is required");

  const year = options.year || await latestYearFromBaseUrl(baseUrl);
  const zip = normalizeZcta(options.zip);
  return getJson(`${baseUrl}/api/v1/${year}/zips/${zip}.json`);
}

export function crimeStatsZipUrl(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");
  if (!options.year) throw new Error("year is required");
  if (!options.zip) throw new Error("zip is required");
  return `${baseUrl}/api/v1/${options.year}/zips/${normalizeZcta(options.zip)}.json`;
}

async function getJson(url) {
  const response = await fetch(url, { mode: "cors" });
  if (!response.ok) throw new Error(`Request failed: ${response.status} ${url}`);
  return response.json();
}

function normalizeZcta(zcta) {
  return String(zcta).trim().padStart(5, "0");
}

function latestYear(manifest) {
  const years = Object.keys(manifest.years || {}).sort();
  if (!years.length) throw new Error("No years are available in the crime data bundle");
  return years[years.length - 1];
}

async function latestYearFromBaseUrl(baseUrl) {
  return latestYear(await getJson(`${baseUrl}/manifest.json`));
}
"""
