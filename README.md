# Crime Index

A local Python pipeline for building ZCTA-level crime counts, rates, analytical indexes, and consumer-friendly 0-100 crime scores from public incident data.

The project runs locally with DuckDB, pandas, GeoPandas, and Shapely. It is designed so the same tables and processing stages can later move to Postgres/PostGIS when API serving, concurrent users, or larger spatial workloads are needed.

## How Data Is Stored

There are two storage layers:

1. Build-time analytical storage lives in `db/crime_index.duckdb`. This local DuckDB file contains raw file metadata, raw JSON payloads, staged incidents, normalized incidents, ZCTA assignments, ACS population, aggregates, score tables, national coverage, and pipeline run history.
2. Runtime serving storage is static yearly files under `data/server/`. These files are generated from `data/exports/` and are intended to be deployed directly to GitHub Pages. The deployed site does not need DuckDB, Python, a live database, or a server process.

The static runtime bundle is the public API surface. Treat the DuckDB file as a reproducible build artifact, not as the production datastore.

## What It Does

The pipeline:

- Ingests raw crime CSV, JSON, GeoJSON, or Parquet files.
- Stores raw file metadata and raw record payloads.
- Normalizes incident dates, offense text, coordinates, addresses, and ZIP/ZCTA fields.
- Classifies offenses into `violent`, `property`, `drug`, `public_order`, `weapons`, `other`, or `unknown`.
- Loads Census ZCTA polygons from local shapefile, GeoJSON, or Parquet data.
- Assigns incidents to Census ZCTAs with a spatial join when coordinates are valid.
- Falls back to a provided ZIP/ZCTA field when coordinates are missing or invalid.
- Loads ACS 5-year ZCTA population data from a local file.
- Aggregates monthly and annual ZCTA counts.
- Calculates rates per 1,000 residents.
- Builds analytical z-score indexes and consumer-facing 0-100 scores.
- Writes CSV, Parquet, GeoJSON, and quality-report outputs.

## Why ZCTAs

ZIP codes are USPS delivery routes, not stable statistical geographies. Census ZIP Code Tabulation Areas, or ZCTAs, are polygon approximations of ZIP service areas and are suitable for Census population joins and spatial statistics.

The canonical key in this project is `zcta`. User-facing copy can say ZIP/ZCTA, but tables and exports use ZCTAs as the statistical geography.

## Local Setup

Python 3.11 or newer is required.

```bash
python -m pip install -e ".[dev]"
```

Or:

```bash
make setup
```

The default configuration points at the downloaded LAPD 2024 crime file, Census 2020 ZCTA boundaries, and ACS 2024 5-year population file. A small sample dataset is also kept under `data/raw/` for quick experiments.

## Data Folders

Place local raw files here:

```text
data/raw/crime/
data/raw/census/
data/raw/geography/
```

Generated outputs are written here:

```text
db/crime_index.duckdb
data/processed/quality_report.json
data/processed/quality_report.md
data/exports/
data/server/
```

## Required Inputs

Crime data should have, at minimum:

- A date or datetime field.
- An offense description or code.
- Latitude and longitude, or a ZIP/ZCTA field.

Census geography should be a ZCTA shapefile, GeoJSON, or GeoParquet file with a ZCTA identifier column such as `ZCTA5CE20`, `GEOID20`, or `GEOID`.

ACS population should be a CSV, JSON, or Parquet file with a ZCTA identifier and population column. The loader supports common ACS columns such as `GEO_ID`, `NAME`, `B01003_001E`, and `B01003_001M`.

## Configure Crime Sources

Edit `config/sources.yaml`:

```yaml
sources:
  my_city:
    file: data/raw/crime/my_city_crime.csv
    jurisdiction_name: My City
    jurisdiction_state: CA
    date_column: occurred_date
    time_column: occurred_time
    offense_column: offense_description
    offense_code_column: offense_code
    latitude_column: latitude
    longitude_column: longitude
    address_column: address
    zip_column: zip_code
    incident_id_column: incident_number
    count_column: null
    source_crs: EPSG:4326
    timezone: America/Los_Angeles
```

Columns can be `null` when a source does not provide them. Records without usable geography are retained and marked `unassigned`. If a source row represents multiple counted incidents or offenses, set `count_column`; otherwise each row defaults to a count of 1.

## Run The Pipeline

Run everything locally:

```bash
python -m crime_index.cli run-all --year 2024
```

Or with Make:

```bash
make run-all
```

On Windows without `make`, run:

```powershell
.\scripts\run_all.ps1 -Year 2024
```

Individual stages:

```bash
python -m crime_index.cli init-db
python -m crime_index.cli download-sources
python -m crime_index.cli ingest-crime --config config/sources.yaml
python -m crime_index.cli normalize-crime
python -m crime_index.cli load-geography --file data/raw/geography/cb_2020_us_zcta520_500k.zip --year 2020
python -m crime_index.cli assign-zctas
python -m crime_index.cli load-population --file data/raw/census/census_reporter_acs2024_zcta_population_us.csv --year 2024
python -m crime_index.cli aggregate --year 2024
python -m crime_index.cli build-index --year 2024 --scope source_universe
python -m crime_index.cli build-national-coverage --year 2024
python -m crime_index.cli build-modeled-baseline --year 2024
python -m crime_index.cli profile --year 2024
python -m crime_index.cli export --year 2024
python -m crime_index.cli export --year 2024 --scope national_modeled_baseline
```

## Offense Classification

Classification rules live in `config/offense_mapping.yaml`.

Processing order:

1. Lowercase offense text.
2. Remove punctuation.
3. Normalize whitespace.
4. Check configured regexes.
5. Check phrase keywords.
6. Check single-word keywords.
7. Resolve competing matches with priority: `violent`, `weapons`, `property`, `drug`, `public_order`, `other`.
8. Assign `unknown` when nothing matches.

Both `offense_group` and `offense_subgroup` are stored.

## ZCTA Assignment

Spatial assignment uses GeoPandas:

- Valid latitude/longitude points are spatially joined to loaded ZCTA polygons.
- Spatial matches receive `assignment_method = spatial_join` and `assignment_confidence = high`.
- Incidents with missing or invalid coordinates and a clean ZIP/ZCTA receive `assignment_method = zip_fallback` and `assignment_confidence = medium`.
- Incidents with neither usable coordinates nor ZIP/ZCTA are kept with `assignment_method = unassigned`.

Invalid coordinates include null values, out-of-range values, and configured placeholders such as `0,0`.

## Rates

Annual category rates use:

```text
category_rate_per_1000 = category_crime_count / population_total * 1000
```

Rates are null when population is missing, zero, or invalid. Monthly tables use the annual population denominator directly.

## 0-100 Crime Scores

Consumer-facing scores are percentile based by default:

```text
0 = lowest relative crime in the comparison universe
100 = highest relative crime in the comparison universe
```

The default comparison scope is `source_universe`, meaning all ZCTAs loaded for the current source set and year.

By default, aggregation includes ZCTAs that have at least one assigned incident in the loaded crime source universe. This avoids scoring every U.S. ZCTA as zero-crime when you load nationwide ACS population for a city-level crime source. Set `aggregation.include_population_only_zctas: true` in `config/settings.yaml` only when your population/geography file has already been clipped to the intended local universe.

The exported score fields include:

- `overall_crime_score_0_100`
- `violent_score_0_100`
- `property_score_0_100`
- `drug_score_0_100`
- `public_order_score_0_100`
- `weapons_score_0_100`
- `other_score_0_100`
- `total_crime_score_0_100`

Scores are relative, not absolute. A score of 80 does not mean an 80% chance of crime. It means the ZCTA is high relative to the selected comparison group.

Labels are also exported:

- `very_low`
- `low`
- `average`
- `high`
- `very_high`
- `unavailable`

## Overall Score

The default overall score weights are:

```yaml
violent: 0.40
property: 0.30
weapons: 0.15
drug: 0.05
public_order: 0.05
other: 0.05
```

Unknown offenses are not included in the overall score by default. Missing category scores are reweighted across available categories and noted in `score_notes`.

## Analytical Indexes

The table also includes internal z-score based fields:

- `violent_z_score`
- `property_z_score`
- `total_z_score`
- `violent_index`
- `property_index`
- `total_index`
- `composite_index`

The composite z-score defaults to:

```text
0.60 * violent_z_score + 0.35 * property_z_score + 0.05 * total_z_score
```

Then:

```text
composite_index = 100 + 15 * composite_z
```

## Confidence Grades

Confidence grades consider population availability, population size, assignment quality, coverage, and low incident counts.

- `A`: strong spatial assignment, population present, enough incidents, population at least 1,000.
- `B`: good spatial assignment, population present, population at least 500.
- `C`: usable but lower stability, often because of low incident counts or fallback assignment.
- `D`: missing population, very low population, or poor coverage.

Small counts are retained but marked in `score_notes`.

## Outputs

The main consumer outputs are:

```text
data/exports/zcta_crime_scores_2024.csv
data/exports/zcta_crime_scores_2024.parquet
data/exports/zcta_crime_scores_2024.geojson
```

With the current 2024 real-data defaults, the workflow also writes:

```text
data/exports/zcta_national_coverage_2024.csv
data/exports/zcta_national_coverage_2024.parquet
data/exports/zcta_crime_scores_2024_national_modeled_baseline.csv
data/exports/zcta_crime_scores_2024_national_modeled_baseline.parquet
data/exports/zcta_crime_scores_2024_national_modeled_baseline.geojson
```

The `source_universe` output contains only ZCTAs with observed local incident data from loaded sources and is marked `coverage_status = observed` and `data_source_type = observed`. The `national_modeled_baseline` output contains every populated ZCTA from the national ACS population file and is marked `coverage_status = national_modeled` and `data_source_type = modeled`. It is a neutral national baseline, not local risk differentiation.

The modeled baseline uses the BJS 2024 national offense rates from *Crime Known to Law Enforcement, 2024*: 370.8 violent offenses and 1,835.1 property offenses per 100,000 persons, stored as 3.708 and 18.351 per 1,000 residents.

After the latest local rebuild, the current observed source universe covers 1,185 ZCTAs from nineteen local incident feeds. The full national coverage export contains 33,772 populated ZCTAs: 1,185 observed rows and 32,587 national modeled rows.

- LAPD 2024 public crime incidents.
- Chicago 2024 public crime incidents.
- San Francisco 2024 public crime incidents.
- Dallas 2024 public RMS incidents.
- NYPD 2024 complaint data.
- Seattle SPD 2024 crime data.
- Philadelphia 2024 crime incidents.
- Denver 2024 crime offenses.
- Colorado Springs 2024 crime-level data.
- Boston 2024 crime incident reports.
- Baltimore 2024 NIBRS Group A crime data.
- Buffalo 2024 crime incidents.
- Cincinnati 2024 PDI crime incidents through the June RMS transition.
- Cincinnati 2024 STARS category offenses after the June RMS transition.
- Kansas City Police Department 2024 crime data.
- District of Columbia 2024 MPD crime incidents.
- Minneapolis 2024 crime data.
- Metro Nashville Police Department 2024 incidents, excluding records marked `UNFOUNDED`.
- Charlotte-Mecklenburg Police Department 2024 incidents, excluding records marked `UNFOUNDED` and NIBRS 800-series non-criminal reports.

All nineteen are configured in `config/sources.yaml` with `download` blocks, so `python -m crime_index.cli download-sources` can recreate the raw files.

## Path To Full-US Indexed Coverage

The product already exports a row for every populated U.S. ZCTA through `zcta_crime_scores_2024_national_modeled_baseline.*`. To move from a modeled full-US product to a higher-confidence observed full-US product, add official local, county, state, or federal incident and agency-level sources in layers:

1. Add high-volume official city/county incident feeds with coordinates first.
2. Add state NIBRS/UCR portals where point-level city data is unavailable.
3. Add FBI/NIBRS agency-level fallback rates for jurisdictions without open incident files.
4. Keep every ZCTA in the national export and use `coverage_status`, `data_source_type`, `source_names`, `assigned_incident_count`, and `confidence_grade` to distinguish observed rows from modeled rows.
5. Rebuild with `python -m crime_index.cli run-all --year 2024`, then review `data/processed/quality_report.md` before using the exports.

Analytical compatibility outputs are also written:

```text
data/exports/zcta_crime_index_2024.csv
data/exports/zcta_crime_index_2024.parquet
data/exports/zcta_crime_index_2024.geojson
```

The quality report is written to:

```text
data/processed/quality_report.md
data/processed/quality_report.json
```

## Static GitHub Pages API

After exports are generated, build the static bundle:

```bash
python -m crime_index.cli build-static-bundle --year 2024
```

Or:

```bash
make build-static-bundle
```

Publish `data/server/` as the GitHub Pages site root. This repo uses a `gh-pages` branch whose root is the contents of `data/server/`:

```bash
git subtree push --prefix data/server origin gh-pages
```

Or:

```bash
make deploy-github-pages
```

The bundle contains:

```text
data/server/
  .nojekyll
  manifest.json
  crime-data-client.js
  2024/
    coverage.json
    national_combined/
      scores.json
    source_universe/
      scores.json
    national_modeled_baseline/
      scores.json
```

Stable URLs after deployment:

```text
https://YOUR_PAGES_HOST/manifest.json
https://YOUR_PAGES_HOST/2024/national_combined/scores.json
https://YOUR_PAGES_HOST/2024/source_universe/scores.json
https://YOUR_PAGES_HOST/2024/national_modeled_baseline/scores.json
https://YOUR_PAGES_HOST/2024/coverage.json
https://YOUR_PAGES_HOST/crime-data-client.js
```

Browser usage from `localhost` or `fmr.fyi`:

```html
<script type="module">
  import { loadCrimeData } from "https://YOUR_PAGES_HOST/crime-data-client.js";

  const crimeData = await loadCrimeData({
    baseUrl: "https://YOUR_PAGES_HOST",
    year: 2024
  });

  console.log(crimeData.getZcta("90210"));
</script>
```

GitHub Pages serves static files with permissive CORS headers. The bundle records the intended consumer origins in `manifest.json` from `config/settings.yaml`:

```yaml
static_bundle:
  allowed_origins:
    - http://localhost:3000
    - http://localhost:5173
    - https://fmr.fyi
    - https://www.fmr.fyi
```

Verify CORS after deployment:

```bash
python -m crime_index.cli check-static-cors --base-url https://YOUR_PAGES_HOST
```

GitHub Pages cannot enforce API-key auth or a private per-origin allowlist for public static files. If the data needs real authentication, usage metering, or origin restriction, put a CDN worker or backend proxy in front of the static bundle. For the current public yearly index, no API key is required.

When both observed and modeled exports are present, the default static scope is `national_combined`: observed source rows replace modeled rows for the same ZCTA, and modeled rows fill the rest of the country. The provenance fields still show whether each row is `observed` or `national_modeled`.

## Add A New Jurisdiction

1. Put the raw crime file in `data/raw/crime/`.
2. Add a source entry to `config/sources.yaml`.
3. Add or adjust offense keywords in `config/offense_mapping.yaml` if the source uses local terminology.
4. Put ZCTA geography in `data/raw/geography/`.
5. Put ACS population data in `data/raw/census/`.
6. Run `python -m crime_index.cli run-all --year YEAR`.
7. Review `data/processed/quality_report.md` for assignment, classification, and population coverage.

## DuckDB To Postgres/PostGIS

The current DuckDB schema stores geometries as WKT for portability. To migrate:

- Create equivalent tables in Postgres.
- Convert `geom_wkt` to PostGIS geometry columns with `ST_GeomFromText`.
- Replace GeoPandas spatial joins with `ST_Within` or `ST_Intersects`.
- Keep the same staged, normalized, aggregate, index, and export table contracts.
- Add production upserts, indexes, and API-serving permissions as needed.

## Limitations And Ethics

This is not an official crime-risk score.

Important limitations:

- Crime data quality varies by jurisdiction.
- Reporting practices vary.
- Police jurisdictions do not align perfectly with ZCTAs.
- ZIPs and ZCTAs are not identical.
- Small-population areas can produce unstable rates.
- Incident coordinates may be generalized, shifted, rounded, or incomplete.
- Scores are relative to the loaded comparison universe and can change when new jurisdictions or years are added.
- Police incident data is not a perfect measure of actual crime.

Do not use these scores for sensitive decisions such as law enforcement targeting, insurance underwriting, lending, housing eligibility, employment screening, individual risk assessment, or other high-impact decisions without expert review.

## Tests

Run:

```bash
python -m pytest
```

The tests cover ZIP cleaning, offense classification, rate safety, index behavior, and ZCTA assignment.
