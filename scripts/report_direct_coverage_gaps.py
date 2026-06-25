from __future__ import annotations

import argparse
import csv
from pathlib import Path

import duckdb


def main() -> None:
    args = parse_args()
    universe_path = args.universe or Path(f"data/exports/zcta_crime_scores_{args.year}_national_modeled_baseline.csv")
    observed_path = args.observed or Path(f"data/exports/zcta_crime_scores_{args.year}.csv")

    require_file(universe_path)
    require_file(observed_path)
    require_file(args.database)

    con = duckdb.connect(str(args.database), read_only=True)
    file_params = {
        "universe": universe_path.as_posix(),
        "observed": observed_path.as_posix(),
    }

    totals = con.execute(
        """
        WITH universe AS (
          SELECT zcta, population_total
          FROM read_csv_auto($universe)
        ),
        observed_file AS (
          SELECT zcta, coverage_status
          FROM read_csv_auto($observed)
        ),
        observed AS (
          SELECT DISTINCT zcta
          FROM observed_file
          WHERE coalesce(coverage_status, 'observed') = 'observed'
        )
        SELECT
          count(*) AS universe_rows,
          count(*) FILTER (WHERE population_total > 0) AS populated_rows,
          (SELECT count(*) FROM observed) AS observed_rows,
          (SELECT count(DISTINCT zcta) FROM observed_file WHERE coverage_status = 'partial_observed') AS partial_rows,
          count(*) - (SELECT count(*) FROM observed) AS missing_rows,
          count(*) FILTER (WHERE population_total > 0)
            - (SELECT count(*) FROM observed) AS missing_populated_rows,
          sum(population_total) FILTER (WHERE population_total > 0) AS population_total,
          sum(CASE WHEN observed.zcta IS NOT NULL AND population_total > 0 THEN population_total ELSE 0 END)
            AS observed_population,
          sum(CASE WHEN observed.zcta IS NULL AND population_total > 0 THEN population_total ELSE 0 END)
            AS missing_population
        FROM universe
        LEFT JOIN observed USING (zcta)
        """,
        file_params,
    ).fetchone()

    state_params = {**file_params, "top": args.top}

    state_rows = con.execute(
        """
        WITH universe AS (
          SELECT zcta, population_total
          FROM read_csv_auto($universe)
          WHERE population_total > 0
        ),
        observed AS (
          SELECT DISTINCT zcta
          FROM read_csv_auto($observed)
          WHERE coalesce(coverage_status, 'observed') = 'observed'
        ),
        zstate AS (
          SELECT
            zcta,
            state_code,
            row_number() OVER (PARTITION BY zcta ORDER BY allocation_weight DESC, state_code) AS rn
          FROM zip_county_mapping
        ),
        base AS (
          SELECT
            universe.zcta,
            universe.population_total,
            coalesce(zstate.state_code, '??') AS state_code,
            CASE WHEN observed.zcta IS NULL THEN 0 ELSE 1 END AS observed
          FROM universe
          LEFT JOIN observed USING (zcta)
          LEFT JOIN zstate ON universe.zcta = zstate.zcta AND zstate.rn = 1
        )
        SELECT
          state_code,
          count(*) AS zctas,
          sum(observed) AS observed_zctas,
          count(*) - sum(observed) AS missing_zctas,
          sum(population_total) AS population,
          sum(CASE WHEN observed = 1 THEN population_total ELSE 0 END) AS observed_population,
          sum(CASE WHEN observed = 0 THEN population_total ELSE 0 END) AS missing_population,
          round(100.0 * sum(observed) / count(*), 1) AS zcta_coverage_pct,
          round(
            100.0 * sum(CASE WHEN observed = 1 THEN population_total ELSE 0 END) / sum(population_total),
            1
          ) AS population_coverage_pct
        FROM base
        GROUP BY state_code
        ORDER BY missing_population DESC
        LIMIT $top
        """,
        state_params,
    ).fetchall()

    target_sql = """
        WITH universe AS (
          SELECT zcta, population_total
          FROM read_csv_auto($universe)
          WHERE population_total >= $min_population
        ),
        observed AS (
          SELECT DISTINCT zcta
          FROM read_csv_auto($observed)
          WHERE coalesce(coverage_status, 'observed') = 'observed'
        ),
        zstate AS (
          SELECT
            zcta,
            state_code,
            row_number() OVER (PARTITION BY zcta ORDER BY allocation_weight DESC, state_code) AS rn
          FROM zip_county_mapping
        ),
        zcounty AS (
          SELECT
            zcta,
            string_agg(DISTINCT county_name, '; ' ORDER BY county_name) AS county_names
          FROM zip_county_mapping
          GROUP BY zcta
        )
        SELECT
          universe.zcta,
          coalesce(zstate.state_code, '??') AS state_code,
          coalesce(zcounty.county_names, '') AS county_names,
          universe.population_total
        FROM universe
        LEFT JOIN observed USING (zcta)
        LEFT JOIN zstate ON universe.zcta = zstate.zcta AND zstate.rn = 1
        LEFT JOIN zcounty ON universe.zcta = zcounty.zcta
        WHERE observed.zcta IS NULL
        ORDER BY universe.population_total DESC, universe.zcta
    """
    target_rows = con.execute(
        f"{target_sql} LIMIT $target_limit",
        {
            **file_params,
            "min_population": args.min_population,
            "target_limit": args.target_limit,
        },
    ).fetchall()

    if args.target_output:
        all_target_rows = con.execute(
            target_sql,
            {**file_params, "min_population": args.min_population},
        ).fetchall()
        write_target_queue(args.target_output, all_target_rows)

    target_totals = con.execute(
        """
        WITH universe AS (
          SELECT zcta, population_total
          FROM read_csv_auto($universe)
          WHERE population_total >= $min_population
        ),
        observed AS (
          SELECT DISTINCT zcta
          FROM read_csv_auto($observed)
          WHERE coalesce(coverage_status, 'observed') = 'observed'
        ),
        zstate AS (
          SELECT
            zcta,
            state_code,
            row_number() OVER (PARTITION BY zcta ORDER BY allocation_weight DESC, state_code) AS rn
          FROM zip_county_mapping
        )
        SELECT
          count(*) AS target_rows,
          count(*) FILTER (WHERE observed.zcta IS NOT NULL) AS covered_rows,
          count(*) FILTER (WHERE observed.zcta IS NULL) AS missing_rows,
          sum(population_total) AS target_population,
          sum(CASE WHEN observed.zcta IS NOT NULL THEN population_total ELSE 0 END) AS covered_population,
          sum(CASE WHEN observed.zcta IS NULL THEN population_total ELSE 0 END) AS missing_population
        FROM universe
        LEFT JOIN observed USING (zcta)
        """,
        {**file_params, "min_population": args.min_population},
    ).fetchone()

    print_report(args.year, totals, state_rows, args.min_population, target_totals, target_rows)
    if args.fail_if_missing and int(float(target_totals[2] or 0)) > 0:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report direct observed ZCTA coverage gaps without using county fallback rows."
    )
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--database", type=Path, default=Path("db/crime_index.duckdb"))
    parser.add_argument("--universe", type=Path, default=None, help="National populated/universe score CSV.")
    parser.add_argument("--observed", type=Path, default=None, help="Direct observed source_universe score CSV.")
    parser.add_argument("--top", type=int, default=20, help="Number of state rows to print.")
    parser.add_argument(
        "--min-population",
        type=int,
        default=50_000,
        help="Population threshold for the high-population coverage target.",
    )
    parser.add_argument(
        "--target-limit",
        type=int,
        default=25,
        help="Number of missing high-population ZCTAs to print.",
    )
    parser.add_argument(
        "--target-output",
        type=Path,
        default=None,
        help="Optional CSV path for the full missing high-population ZCTA target queue.",
    )
    parser.add_argument(
        "--fail-if-missing",
        action="store_true",
        help="Exit nonzero when any ZCTA at or above --min-population is missing direct observed data.",
    )
    return parser.parse_args()


def require_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")


def print_report(
    year: int,
    totals: tuple[object, ...],
    state_rows: list[tuple[object, ...]],
    min_population: int,
    target_totals: tuple[object, ...],
    target_rows: list[tuple[object, ...]],
) -> None:
    (
        universe_rows,
        populated_rows,
        observed_rows,
        partial_rows,
        missing_rows,
        missing_populated_rows,
        population_total,
        observed_population,
        missing_population,
    ) = totals
    population_coverage = safe_pct(float(observed_population or 0), float(population_total or 0))

    print(f"# Direct ZCTA Coverage Gaps ({year})")
    print()
    print(f"- Universe rows: {fmt(universe_rows)}")
    print(f"- Populated universe rows: {fmt(populated_rows)}")
    print(f"- Direct observed ZCTAs: {fmt(observed_rows)}")
    print(f"- Partial direct rows excluded from covered counts: {fmt(partial_rows)}")
    print(f"- Missing rows, including zero-population rows: {fmt(missing_rows)}")
    print(f"- Missing populated ZCTAs: {fmt(missing_populated_rows)}")
    print(f"- Observed population: {fmt(observed_population)}")
    print(f"- Missing population: {fmt(missing_population)}")
    print(f"- Population coverage: {population_coverage:.1f}%")
    print()
    print("## Largest State Gaps By Missing Population")
    print()
    print("| State | ZCTAs | Observed | Missing | Missing Pop. | ZCTA Coverage | Pop. Coverage |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in state_rows:
        state, zctas, observed, missing, _population, _observed_pop, missing_pop, zcta_pct, pop_pct = row
        print(
            f"| {state} | {fmt(zctas)} | {fmt(observed)} | {fmt(missing)} | "
            f"{fmt(missing_pop)} | {float(zcta_pct or 0):.1f}% | {float(pop_pct or 0):.1f}% |"
        )
    print()
    print(f"## High-Population Target: >= {fmt(min_population)} Residents")
    print()
    target_rows_total, covered_rows, missing_rows, target_population, covered_population, missing_population = target_totals
    target_coverage = safe_pct(float(covered_rows or 0), float(target_rows_total or 0))
    target_population_coverage = safe_pct(float(covered_population or 0), float(target_population or 0))
    print(f"- Target ZCTAs: {fmt(target_rows_total)}")
    print(f"- Covered target ZCTAs: {fmt(covered_rows)}")
    print(f"- Missing target ZCTAs: {fmt(missing_rows)}")
    print(f"- Target ZCTA coverage: {target_coverage:.1f}%")
    print(f"- Target population coverage: {target_population_coverage:.1f}%")
    print(f"- Missing target population: {fmt(missing_population)}")
    if target_rows:
        print()
        print("| Missing ZCTA | State | County Names | Population |")
        print("| --- | --- | --- | ---: |")
        for zcta, state, county_names, population in target_rows:
            print(f"| {str(zcta).zfill(5)} | {state} | {county_names} | {fmt(population)} |")


def write_target_queue(path: Path, rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["priority_rank", "zcta", "state_code", "county_names", "population_total"])
        for index, (zcta, state, county_names, population) in enumerate(rows, start=1):
            writer.writerow([index, str(zcta).zfill(5), state, county_names, int(float(population or 0))])


def fmt(value: object) -> str:
    if value is None:
        return "0"
    return f"{int(float(value)):,}"


def safe_pct(numerator: float, denominator: float) -> float:
    return 0.0 if denominator <= 0 else 100.0 * numerator / denominator


if __name__ == "__main__":
    main()
