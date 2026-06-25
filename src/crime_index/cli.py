from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from crime_index.config import load_settings
from crime_index.config import load_sources
from crime_index.coverage import build_national_coverage, export_national_coverage, load_source_coverage
from crime_index.db import get_connection, init_db as init_database
from crime_index.export.export_tables import export_outputs
from crime_index.geo.zcta_assignment import assign_zctas as assign_zctas_stage
from crime_index.ingest.census_loader import load_population as load_population_stage
from crime_index.ingest.census_reporter import fetch_census_reporter_zcta_population
from crime_index.ingest.crime_loader import ingest_crime as ingest_crime_stage
from crime_index.ingest.geography_loader import load_geography as load_geography_stage
from crime_index.ingest.source_downloader import download_configured_sources
from crime_index.logging_config import configure_logging
from crime_index.modeling import build_modeled_baseline
from crime_index.normalize.normalize_crime import normalize_crime as normalize_crime_stage
from crime_index.quality.profiling import profile as profile_stage
from crime_index.static_bundle import build_static_bundle as build_static_bundle_stage
from crime_index.static_bundle import check_static_cors as check_static_cors_stage
from crime_index.transform.aggregate import aggregate_crime
from crime_index.transform.index import build_index as build_index_stage
from crime_index.utils.time_utils import utc_now_naive

app = typer.Typer(help="Local ZCTA crime index pipeline.")
console = Console()
LOGGER = logging.getLogger(__name__)

REBUILD_OUTPUT_TABLES = [
    "source_coverage",
    "zcta_geometries",
    "incident_zcta_assignment",
    "acs_zcta_population",
    "zcta_crime_annual",
    "zcta_crime_monthly",
    "zcta_crime_index",
    "zcta_national_coverage",
]


@app.callback()
def main(log_level: str = typer.Option("INFO", "--log-level", help="Python logging level.")) -> None:
    configure_logging(log_level)


@app.command("init-db")
def init_db() -> None:
    init_database()
    console.print("[green]Initialized DuckDB schema.[/green]")


@app.command("ingest-crime")
def ingest_crime(config: Path = typer.Option(Path("config/sources.yaml"), "--config")) -> None:
    results = ingest_crime_stage(config)
    _print_mapping("Crime ingestion", results)


@app.command("download-sources")
def download_sources(
    config: Path = typer.Option(Path("config/sources.yaml"), "--config"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing raw source files."),
) -> None:
    results = download_configured_sources(config, force=force)
    _print_mapping("Source downloads", results)


@app.command("normalize-crime")
def normalize_crime() -> None:
    row_count = normalize_crime_stage()
    console.print(f"[green]Normalized {row_count} crime incidents.[/green]")


@app.command("load-geography")
def load_geography(
    file: Path | None = typer.Option(None, "--file", help="ZCTA shapefile, GeoJSON, or Parquet."),
    year: int | None = typer.Option(None, "--year", help="Geography source year."),
) -> None:
    settings = load_settings()
    file = file or Path(settings.get("geography", {}).get("default_file", "data/raw/geography/sample_zcta.geojson"))
    year = year or int(settings.get("geography", {}).get("default_year", 2024))
    row_count = load_geography_stage(file, year, settings=settings)
    console.print(f"[green]Loaded {row_count} ZCTA geometries.[/green]")


@app.command("assign-zctas")
def assign_zctas() -> None:
    summary = assign_zctas_stage()
    _print_mapping("ZCTA assignment", summary)


@app.command("load-population")
def load_population(
    file: Path | None = typer.Option(None, "--file", help="ACS ZCTA population CSV/JSON/Parquet."),
    year: int | None = typer.Option(None, "--year", help="ACS year."),
) -> None:
    settings = load_settings()
    file = file or Path(settings.get("population", {}).get("default_file", "data/raw/census/sample_acs_zcta_population.csv"))
    year = year or int(settings.get("population", {}).get("default_year", 2023))
    row_count = load_population_stage(file, year)
    console.print(f"[green]Loaded {row_count} population rows.[/green]")


@app.command("fetch-census-reporter-population")
def fetch_census_reporter_population(
    output: Path = typer.Option(Path("data/raw/census/census_reporter_acs2024_zcta_population_us.csv"), "--output"),
    release: str = typer.Option("latest", "--release"),
) -> None:
    result = fetch_census_reporter_zcta_population(output, release)
    _print_mapping("Census Reporter population fetch", result)


@app.command("load-source-coverage")
def load_source_coverage_command(config: Path = typer.Option(Path("config/sources.yaml"), "--config")) -> None:
    row_count = load_source_coverage(load_sources(config))
    console.print(f"[green]Loaded {row_count} source coverage rows.[/green]")


@app.command("build-national-coverage")
def build_national_coverage_command(year: int = typer.Option(..., "--year")) -> None:
    row_count = build_national_coverage(year)
    written = export_national_coverage(year)
    console.print(f"[green]Built {row_count} national coverage rows.[/green]")
    _print_mapping("National coverage exports", written)


@app.command("build-modeled-baseline")
def build_modeled_baseline_command(
    year: int = typer.Option(..., "--year"),
    scope: str | None = typer.Option(None, "--scope"),
) -> None:
    row_count = build_modeled_baseline(year, comparison_scope=scope)
    console.print(f"[green]Built {row_count} modeled baseline rows.[/green]")


@app.command("aggregate")
def aggregate(year: int = typer.Option(..., "--year")) -> None:
    summary = aggregate_crime(year)
    _print_mapping("Aggregation", summary)


@app.command("build-index")
def build_index(
    year: int = typer.Option(..., "--year"),
    scope: str = typer.Option("source_universe", "--scope"),
) -> None:
    row_count = build_index_stage(year, scope)
    console.print(f"[green]Built {row_count} index rows.[/green]")


@app.command("profile")
def profile(year: int | None = typer.Option(None, "--year")) -> None:
    result = profile_stage(year)
    files = result["files"]
    console.print(f"[green]Wrote quality report to {files['markdown']} and {files['json']}.[/green]")


@app.command("export")
def export(
    year: int = typer.Option(..., "--year"),
    scope: str = typer.Option("source_universe", "--scope"),
) -> None:
    written = export_outputs(year, comparison_scope=scope)
    _print_mapping("Exports", written)


@app.command("build-static-bundle")
def build_static_bundle(
    export_dir: Path = typer.Option(Path("data/exports"), "--export-dir", help="Directory containing yearly CSV exports."),
    output_dir: Path = typer.Option(Path("data/server"), "--output-dir", help="Static bundle directory for GitHub Pages."),
    year: list[int] | None = typer.Option(None, "--year", help="Year to include. Can be repeated."),
    scope: list[str] | None = typer.Option(None, "--scope", help="Scope to include. Can be repeated."),
) -> None:
    settings = load_settings()
    allowed_origins = settings.get("static_bundle", {}).get("allowed_origins", [])
    manifest = build_static_bundle_stage(
        export_dir=export_dir,
        output_dir=output_dir,
        years=year,
        scopes=scope,
        allowed_origins=allowed_origins,
    )
    console.print(f"[green]Built static bundle in {output_dir}.[/green]")
    console.print_json(json.dumps(manifest))


@app.command("check-static-cors")
def check_static_cors(
    base_url: str = typer.Option(..., "--base-url", help="Published GitHub Pages data root."),
    origin: list[str] | None = typer.Option(None, "--origin", help="Origin to verify. Can be repeated."),
) -> None:
    settings = load_settings()
    origins = origin or settings.get("static_bundle", {}).get("allowed_origins", [])
    results = check_static_cors_stage(base_url, origins)
    _print_mapping("Static CORS", {item["origin"]: item for item in results})


@app.command("run-all")
def run_all(
    year: int | None = typer.Option(None, "--year", help="Crime/ACS year to process."),
    scope: str = typer.Option("source_universe", "--scope", help="Comparison scope for scores."),
    sources_config: Path = typer.Option(Path("config/sources.yaml"), "--config", help="Crime source config."),
) -> None:
    settings = load_settings()
    year = year or int(settings.get("population", {}).get("default_year", 2023))
    geography_file = Path(settings.get("geography", {}).get("default_file", "data/raw/geography/sample_zcta.geojson"))
    geography_year = int(settings.get("geography", {}).get("default_year", 2024))
    population_file = Path(settings.get("population", {}).get("default_file", "data/raw/census/sample_acs_zcta_population.csv"))
    command = f"run-all --year {year} --scope {scope} --config {sources_config}"

    run_id = _start_pipeline_run(command, settings)
    try:
        init_database()
        console.print("[cyan]1/14 initialized database[/cyan]")
        _clear_rebuild_outputs()
        ingest_results = ingest_crime_stage(sources_config)
        console.print(f"[cyan]2/14 ingested crime: {ingest_results}[/cyan]")
        source_coverage_count = load_source_coverage(load_sources(sources_config))
        console.print(f"[cyan]3/14 loaded source coverage: {source_coverage_count} rows[/cyan]")
        normalized_count = normalize_crime_stage(settings=settings)
        console.print(f"[cyan]4/14 normalized crime: {normalized_count} rows[/cyan]")
        geography_count = load_geography_stage(geography_file, geography_year, settings=settings)
        console.print(f"[cyan]5/14 loaded geography: {geography_count} rows[/cyan]")
        assignment_summary = assign_zctas_stage(settings=settings)
        console.print(f"[cyan]6/14 assigned ZCTAs: {assignment_summary}[/cyan]")
        population_count = load_population_stage(population_file, year)
        console.print(f"[cyan]7/14 loaded population: {population_count} rows[/cyan]")
        aggregate_summary = aggregate_crime(year, settings=settings)
        console.print(f"[cyan]8/14 aggregated: {aggregate_summary}[/cyan]")
        index_count = build_index_stage(year, scope, settings=settings)
        console.print(f"[cyan]9/14 built observed scores: {index_count} rows[/cyan]")
        coverage_count = build_national_coverage(year)
        coverage_exports = export_national_coverage(year)
        console.print(f"[cyan]10/14 built national coverage: {coverage_count} rows {coverage_exports}[/cyan]")
        modeled_scope = settings.get("modeled_baseline", {}).get("comparison_scope", "national_modeled_baseline")
        modeled_count = build_modeled_baseline(year, comparison_scope=modeled_scope, settings=settings)
        console.print(f"[cyan]11/14 built modeled baseline: {modeled_count} rows[/cyan]")
        profile_result = profile_stage(year)
        console.print(f"[cyan]12/14 profiled: {profile_result['files']}[/cyan]")
        written = export_outputs(year, comparison_scope=scope)
        console.print(f"[cyan]13/14 exported observed scores: {written}[/cyan]")
        modeled_written = export_outputs(year, comparison_scope=modeled_scope)
        console.print(f"[cyan]14/14 exported modeled scores: {modeled_written}[/cyan]")
        _complete_pipeline_run(run_id, "completed", "run-all completed")
    except Exception as exc:
        _complete_pipeline_run(run_id, "failed", str(exc))
        raise


def _print_mapping(title: str, mapping: dict[str, Any]) -> None:
    table = Table(title=title)
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in mapping.items():
        table.add_row(str(key), str(value))
    console.print(table)


def _clear_rebuild_outputs(database_path: str | Path | None = None) -> None:
    init_database(database_path)
    with get_connection(database_path) as con:
        for table in REBUILD_OUTPUT_TABLES:
            con.execute(f"DELETE FROM {table}")


def _start_pipeline_run(command: str, settings: dict[str, Any]) -> str:
    init_database()
    run_id = str(uuid.uuid4())
    with get_connection() as con:
        run = pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "started_at": utc_now_naive(),
                    "completed_at": None,
                    "status": "running",
                    "command": command,
                    "config_snapshot_json": json.dumps(settings, sort_keys=True, default=str),
                    "notes": None,
                }
            ]
        )
        con.register("_pipeline_run", run)
        con.execute("INSERT INTO pipeline_runs SELECT * FROM _pipeline_run")
        con.unregister("_pipeline_run")
    return run_id


def _complete_pipeline_run(run_id: str, status: str, notes: str) -> None:
    with get_connection() as con:
        con.execute(
            """
            UPDATE pipeline_runs
            SET completed_at = ?, status = ?, notes = ?
            WHERE run_id = ?
            """,
            [utc_now_naive(), status, notes, run_id],
        )


if __name__ == "__main__":
    app()
