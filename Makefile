PYTHON ?= python

.PHONY: setup init-db ingest-crime normalize-crime load-geography assign-zctas load-population aggregate build-index profile export build-static-bundle coverage-gaps deploy-github-pages check-static-cors run-all test

setup:
	$(PYTHON) -m pip install -e ".[dev]"

init-db:
	$(PYTHON) -m crime_index.cli init-db

ingest-crime:
	$(PYTHON) -m crime_index.cli ingest-crime --config config/sources.yaml

normalize-crime:
	$(PYTHON) -m crime_index.cli normalize-crime

load-geography:
	$(PYTHON) -m crime_index.cli load-geography --file data/raw/geography/cb_2020_us_zcta520_500k.zip --year 2020

assign-zctas:
	$(PYTHON) -m crime_index.cli assign-zctas

load-population:
	$(PYTHON) -m crime_index.cli load-population --file data/raw/census/census_reporter_acs2024_zcta_population_us.csv --year 2024

aggregate:
	$(PYTHON) -m crime_index.cli aggregate --year 2024

build-index:
	$(PYTHON) -m crime_index.cli build-index --year 2024 --scope source_universe

profile:
	$(PYTHON) -m crime_index.cli profile --year 2024

export:
	$(PYTHON) -m crime_index.cli export --year 2024

build-static-bundle:
	$(PYTHON) -m crime_index.cli build-static-bundle --year 2024

coverage-gaps:
	$(PYTHON) scripts/report_direct_coverage_gaps.py --year 2024 --target-output data/processed/direct_zcta_targets_2024_minpop_50000.csv

deploy-github-pages:
	git subtree push --prefix data/server origin gh-pages

check-static-cors:
	$(PYTHON) -m crime_index.cli check-static-cors --base-url $(BASE_URL)

run-all:
	$(PYTHON) -m crime_index.cli run-all --year 2024

test:
	$(PYTHON) -m pytest
