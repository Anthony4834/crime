const COUNT_FIELDS = [
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
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
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
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");

  const manifest = await getJson(`${baseUrl}/manifest.json`);
  const year = String(options.year || latestYear(manifest));
  const coverageInfo = manifest.years?.[year]?.coverage;
  if (!coverageInfo) throw new Error(`Coverage is not available for ${year}`);
  return getJson(`${baseUrl}/${coverageInfo.path}`);
}

export async function getCrimeStatsForZip(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");
  if (!options.zip) throw new Error("zip is required");

  const year = options.year || await latestYearFromBaseUrl(baseUrl);
  const zip = normalizeZcta(options.zip);
  return getJson(`${baseUrl}/api/v1/${year}/zips/${zip}.json`);
}

export async function getCrimeStatsForZips(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
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

export async function getCrimeStatsForCounty(options = {}) {
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
  if (!baseUrl) throw new Error("baseUrl is required");
  if (!options.countyFips) throw new Error("countyFips is required");

  const year = options.year || await latestYearFromBaseUrl(baseUrl);
  const countyFips = String(options.countyFips).trim().padStart(5, "0");
  return getJson(`${baseUrl}/api/v1/${year}/counties/${countyFips}.json`);
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
  const baseUrl = (options.baseUrl || "").replace(/\/$/, "");
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
