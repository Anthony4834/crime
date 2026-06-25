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
