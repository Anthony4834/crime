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
OBSERVED_SCOPE = "source_universe"

INT_FIELDS = {
    "year",
    "population_total",
    "source_count",
    "county_count",
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
COUNT_FIELDS = [
    "total_crime_count",
    "violent_crime_count",
    "property_crime_count",
    "drug_crime_count",
    "public_order_crime_count",
    "weapons_crime_count",
    "other_crime_count",
    "unknown_crime_count",
]
SCORE_FIELDS = [
    "overall_crime_score_0_100",
    "violent_score_0_100",
    "property_score_0_100",
    "drug_score_0_100",
    "public_order_score_0_100",
    "weapons_score_0_100",
    "other_score_0_100",
    "total_crime_score_0_100",
]
RATE_FIELDS = {
    "total_crime_count": "total_rate_per_1000",
    "violent_crime_count": "violent_rate_per_1000",
    "property_crime_count": "property_rate_per_1000",
    "drug_crime_count": "drug_rate_per_1000",
    "public_order_crime_count": "public_order_rate_per_1000",
    "weapons_crime_count": "weapons_rate_per_1000",
    "other_crime_count": "other_rate_per_1000",
    "unknown_crime_count": "unknown_rate_per_1000",
}


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
    _clear_generated_bundle_files(output_dir)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_type": "yearly_static_files",
        "generated_at": utc_now_naive().isoformat(),
        "cors": {
            "hosting": "github_pages",
            "note": "GitHub Pages serves static assets with permissive CORS. These origins are the intended consumers, not an enforceable allowlist.",
            "intended_consumer_origins": allowed_origins or [],
        },
        "geography_resolution": {
            "runtime_scope": "zcta_only",
            "note": "Consumers own address, city, county, metro, and custom-area mapping. Pass resolved ZIP/ZCTA keys to the ZIP API or client-side group analyzer.",
        },
        "runtime_data_policy": {
            "published_scope": OBSERVED_SCOPE,
            "fallbacks": "disabled",
            "note": "The public API only serves complete direct granular observed ZCTA records. It excludes county-allocated, national-modeled, and partial-observed rows.",
        },
        "years": {},
    }

    for csv_path in sorted(export_dir.glob("zcta_crime_scores_*.csv")):
        match = SCORES_FILE_PATTERN.match(csv_path.name)
        if not match:
            continue
        year = match.group("year")
        scope = match.group("scope") or "source_universe"
        if scope != OBSERVED_SCOPE:
            continue
        if year_filter and year not in year_filter:
            continue
        if scope_filter and scope not in scope_filter:
            continue

        records = [
            record
            for record in _read_csv_records(csv_path)
            if record.get("coverage_status") in {None, "", "observed"}
        ]
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
        year_entry["zip_api"] = _write_zip_api(output_dir, year, records, scope=scope)
        year_entry["coverage"] = _write_observed_coverage(output_dir, year, records)

    for year_entry in manifest["years"].values():
        scopes_for_year = year_entry.get("scopes", {})
        if OBSERVED_SCOPE in scopes_for_year:
            year_entry["default_scope"] = OBSERVED_SCOPE
        elif scopes_for_year:
            year_entry["default_scope"] = sorted(scopes_for_year)[0]

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    (output_dir / "crime-data-client.js").write_text(_client_js(), encoding="utf-8")
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def _write_zip_api(output_dir: Path, year: str, records: list[dict[str, Any]], scope: str) -> dict[str, Any]:
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
        "scope": scope,
        "path_template": f"{base_path.as_posix()}/{{zip}}.json",
        "row_count": len(records),
    }


def _write_observed_coverage(output_dir: Path, year: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    relative_path = Path(year) / "coverage.json"
    output_path = output_dir / relative_path
    coverage_records = [
        {
            "zcta": str(record["zcta"]).zfill(5),
            "year": int(record.get("year") or year),
            "population_total": record.get("population_total"),
            "coverage_status": record.get("coverage_status"),
            "data_source_type": record.get("data_source_type"),
            "coverage_notes": None,
        }
        for record in records
    ]
    _write_json(
        output_path,
        {
            "year": int(year),
            "scope": OBSERVED_SCOPE,
            "row_count": len(coverage_records),
            "records": coverage_records,
        },
    )
    return {
        "path": relative_path.as_posix(),
        "row_count": len(coverage_records),
        "sha256": _sha256(output_path),
        "coverage_status_counts": _counts(coverage_records, "coverage_status"),
        "data_source_type_counts": _counts(coverage_records, "data_source_type"),
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


def _clear_generated_bundle_files(output_dir: Path) -> None:
    api_dir = output_dir / "api" / "v1"
    if api_dir.exists():
        shutil.rmtree(api_dir)
    children = output_dir.iterdir() if output_dir.exists() else []
    for child in children:
        if child.is_dir() and re.fullmatch(r"\d{4}", child.name):
            shutil.rmtree(child)
    for filename in ["manifest.json", "crime-data-client.js", ".nojekyll"]:
        path = output_dir / filename
        if path.exists():
            path.unlink()


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
    return """const COUNT_FIELDS = [
  "total_crime_count",
  "violent_crime_count",
  "property_crime_count",
  "drug_crime_count",
  "public_order_crime_count",
  "weapons_crime_count",
  "other_crime_count",
  "unknown_crime_count"
];

const SCORE_FIELDS = [
  "overall_crime_score_0_100",
  "violent_score_0_100",
  "property_score_0_100",
  "drug_score_0_100",
  "public_order_score_0_100",
  "weapons_score_0_100",
  "other_score_0_100",
  "total_crime_score_0_100"
];

const RATE_FIELDS = {
  total_crime_count: "total_rate_per_1000",
  violent_crime_count: "violent_rate_per_1000",
  property_crime_count: "property_rate_per_1000",
  drug_crime_count: "drug_rate_per_1000",
  public_order_crime_count: "public_order_rate_per_1000",
  weapons_crime_count: "weapons_rate_per_1000",
  other_crime_count: "other_rate_per_1000",
  unknown_crime_count: "unknown_rate_per_1000"
};

export async function loadCrimeData(options = {}) {
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

export async function getCrimeStatsForZips(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");
  if (!Array.isArray(options.zips) || options.zips.length === 0) {
    throw new Error("zips must be a non-empty array");
  }

  const year = options.year || await latestYearFromBaseUrl(baseUrl);
  const requestedZips = [...new Set(options.zips.map(normalizeZcta))].sort();
  const results = await Promise.all(requestedZips.map(async (zip) => {
    const url = crimeStatsZipUrl({ baseUrl, year, zip });
    const response = await fetch(url, { mode: "cors" });
    if (response.status === 404) return { zip, missing: true };
    if (!response.ok) throw new Error(`Request failed: ${response.status} ${url}`);
    return { zip, record: await response.json() };
  }));

  const records = results.filter((result) => result.record).map((result) => result.record);
  const missingZips = results.filter((result) => result.missing).map((result) => result.zip);
  return analyzeCrimeStatsGroup(records, {
    year,
    label: options.label || null,
    requestedZips,
    missingZips,
    includeMembers: options.includeMembers !== false
  });
}

export function analyzeCrimeStatsGroup(records, options = {}) {
  const uniqueRecords = dedupeRecords(records);
  const requestedZips = options.requestedZips || uniqueRecords.map((record) => normalizeZcta(record.zcta));
  const missingZips = options.missingZips || [];
  const populationTotal = sumNumeric(uniqueRecords, "population_total");
  const counts = {};
  const ratesPer1000 = {};

  for (const field of COUNT_FIELDS) {
    const value = sumNumeric(uniqueRecords, field);
    counts[field] = hasAnyNumeric(uniqueRecords, field) ? value : null;
    ratesPer1000[RATE_FIELDS[field]] = counts[field] === null || populationTotal <= 0
      ? null
      : round((counts[field] / populationTotal) * 1000, 3);
  }

  const scores = {};
  const labels = {};
  for (const field of SCORE_FIELDS) {
    const score = weightedAverage(uniqueRecords, field, "population_total");
    scores[field] = score === null ? null : round(score, 1);
    labels[field.replace("_0_100", "_label")] = scoreLabel(score);
  }

  const foundZips = uniqueRecords.map((record) => normalizeZcta(record.zcta)).sort();
  const coverageStatusCounts = countBy(uniqueRecords, "coverage_status");
  const dataSourceTypeCounts = countBy(uniqueRecords, "data_source_type");
  const confidenceGradeCounts = countBy(uniqueRecords, "confidence_grade");
  const sourceNames = sortedSourceNames(uniqueRecords);

  const result = {
    api_version: "v1",
    analysis_type: "zip_group",
    aggregation_method: "counts summed; rates recomputed from summed counts and population; scores are population-weighted averages of ZIP/ZCTA scores",
    year: options.year || inferYear(uniqueRecords),
    label: options.label || null,
    requested_zips: requestedZips,
    found_zips: foundZips,
    missing_zips: missingZips,
    requested_zip_count: requestedZips.length,
    found_zip_count: foundZips.length,
    missing_zip_count: missingZips.length,
    population_total: populationTotal,
    counts,
    rates_per_1000: ratesPer1000,
    scores_0_100: scores,
    score_labels: labels,
    coverage: {
      coverage_status_counts: coverageStatusCounts,
      data_source_type_counts: dataSourceTypeCounts,
      direct_observed_zip_count: coverageStatusCounts.observed || 0,
      county_observed_zip_count: coverageStatusCounts.county_observed_allocated || 0,
      observed_zip_count: (coverageStatusCounts.observed || 0) + (coverageStatusCounts.county_observed_allocated || 0),
      modeled_zip_count: coverageStatusCounts.national_modeled || 0
    },
    confidence_grade_counts: confidenceGradeCounts,
    source_names: sourceNames,
    notes: [
      "ZIP inputs are normalized to 5-digit ZCTA keys.",
      "ZIPs and Census ZCTAs are not identical.",
      "Group scores are weighted averages of ZIP/ZCTA scores, not freshly ranked county percentiles."
    ]
  };
  if (options.includeMembers !== false) result.members = uniqueRecords;
  return result;
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

function dedupeRecords(records) {
  const byZcta = new Map();
  for (const record of records || []) {
    if (!record || record.zcta == null) continue;
    byZcta.set(normalizeZcta(record.zcta), { ...record, zcta: normalizeZcta(record.zcta) });
  }
  return [...byZcta.values()].sort((a, b) => String(a.zcta).localeCompare(String(b.zcta)));
}

function sumNumeric(records, field) {
  return records.reduce((sum, record) => {
    const value = toNumber(record[field]);
    return value === null ? sum : sum + value;
  }, 0);
}

function hasAnyNumeric(records, field) {
  return records.some((record) => toNumber(record[field]) !== null);
}

function weightedAverage(records, valueField, weightField) {
  let weightedSum = 0;
  let weightSum = 0;
  let fallbackSum = 0;
  let fallbackCount = 0;
  for (const record of records) {
    const value = toNumber(record[valueField]);
    if (value === null) continue;
    const weight = toNumber(record[weightField]);
    if (weight !== null && weight > 0) {
      weightedSum += value * weight;
      weightSum += weight;
    } else {
      fallbackSum += value;
      fallbackCount += 1;
    }
  }
  if (weightSum > 0) return weightedSum / weightSum;
  return fallbackCount ? fallbackSum / fallbackCount : null;
}

function countBy(records, field) {
  return records.reduce((counts, record) => {
    const key = record[field] == null || record[field] === "" ? "unavailable" : String(record[field]);
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
}

function sortedSourceNames(records) {
  const names = new Set();
  for (const record of records) {
    for (const name of String(record.source_names || "").split("|")) {
      const trimmed = name.trim();
      if (trimmed) names.add(trimmed);
    }
  }
  return [...names].sort();
}

function inferYear(records) {
  const years = [...new Set(records.map((record) => record.year).filter(Boolean))].sort();
  return years.length === 1 ? years[0] : null;
}

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function round(value, digits) {
  const factor = 10 ** digits;
  return Math.round((value + Number.EPSILON) * factor) / factor;
}

function scoreLabel(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return "unavailable";
  const value = Number(score);
  if (value < 20) return "very_low";
  if (value < 40) return "low";
  if (value < 60) return "average";
  if (value < 80) return "high";
  return "very_high";
}
"""
