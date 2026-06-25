from __future__ import annotations

from pathlib import Path

import duckdb

from crime_index.config import get_database_path, load_settings


def get_connection(database_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    if database_path is None:
        database_path = get_database_path(load_settings())
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def init_db(database_path: str | Path | None = None) -> None:
    with get_connection(database_path) as con:
        create_tables(con)


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_crime_files (
            raw_file_id TEXT,
            source_name TEXT,
            file_path TEXT,
            file_format TEXT,
            file_size_bytes BIGINT,
            file_modified_at TIMESTAMP,
            ingested_at TIMESTAMP,
            row_count BIGINT,
            file_hash TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_crime_records (
            raw_record_id TEXT,
            raw_file_id TEXT,
            source_name TEXT,
            source_row_number BIGINT,
            raw_payload_json TEXT,
            ingested_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS staged_crime_incidents (
            incident_id TEXT,
            source_name TEXT,
            jurisdiction_name TEXT,
            jurisdiction_state TEXT,
            source_row_number BIGINT,
            source_incident_id TEXT,
            incident_count BIGINT,
            occurred_at TIMESTAMP,
            occurred_date DATE,
            occurred_year INTEGER,
            occurred_month INTEGER,
            offense_raw TEXT,
            offense_code_raw TEXT,
            address_raw TEXT,
            zip_raw TEXT,
            latitude_raw DOUBLE,
            longitude_raw DOUBLE,
            source_crs TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS normalized_crime_incidents (
            incident_id TEXT,
            source_name TEXT,
            incident_count BIGINT,
            jurisdiction_name TEXT,
            jurisdiction_state TEXT,
            occurred_at TIMESTAMP,
            occurred_date DATE,
            occurred_year INTEGER,
            occurred_month INTEGER,
            offense_raw TEXT,
            offense_normalized TEXT,
            offense_group TEXT,
            offense_subgroup TEXT,
            is_violent BOOLEAN,
            is_property BOOLEAN,
            address_normalized TEXT,
            zip_raw TEXT,
            zcta_from_zip TEXT,
            latitude DOUBLE,
            longitude DOUBLE,
            geom_wkt TEXT,
            data_quality_score DOUBLE,
            normalization_notes TEXT,
            created_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS zcta_geometries (
            zcta TEXT,
            geoid TEXT,
            state_fips TEXT,
            land_area DOUBLE,
            water_area DOUBLE,
            centroid_lat DOUBLE,
            centroid_lon DOUBLE,
            geom_wkt TEXT,
            source_year INTEGER,
            source_file TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_zcta_assignment (
            incident_id TEXT,
            zcta TEXT,
            assignment_method TEXT,
            assignment_confidence TEXT,
            assignment_notes TEXT,
            assigned_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS acs_zcta_population (
            zcta TEXT,
            year INTEGER,
            population_total BIGINT,
            population_margin_error BIGINT,
            source TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS source_coverage (
            source_name TEXT,
            source_type TEXT,
            coverage_level TEXT,
            coverage_area_name TEXT,
            coverage_state TEXT,
            source_url TEXT,
            source_year INTEGER,
            data_start_date DATE,
            data_end_date DATE,
            update_cadence TEXT,
            has_point_coordinates BOOLEAN,
            coordinate_quality TEXT,
            offense_mapping_quality TEXT,
            coverage_notes TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS zcta_national_coverage (
            zcta TEXT,
            year INTEGER,
            population_total BIGINT,
            source_count BIGINT,
            source_names TEXT,
            assigned_incident_count BIGINT,
            spatial_incident_count BIGINT,
            coverage_status TEXT,
            data_source_type TEXT,
            coverage_notes TEXT,
            created_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS zip_county_mapping (
            zcta TEXT,
            county_fips TEXT,
            county_name TEXT,
            state_code TEXT,
            state_name TEXT,
            allocation_weight DOUBLE,
            source TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fbi_cde_agencies (
            ori TEXT,
            state_code TEXT,
            state_name TEXT,
            agency_name TEXT,
            agency_type_name TEXT,
            counties TEXT,
            county_fips TEXT,
            county_name TEXT,
            is_nibrs BOOLEAN,
            latitude DOUBLE,
            longitude DOUBLE,
            nibrs_start_date DATE,
            source_json TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fbi_cde_cius_agency_offenses (
            year INTEGER,
            table_number TEXT,
            table_name TEXT,
            state_code TEXT,
            state_name TEXT,
            agency_label TEXT,
            agency_type TEXT,
            county_fips TEXT,
            county_name TEXT,
            population_reported BIGINT,
            violent_crime_count BIGINT,
            property_crime_count BIGINT,
            murder_count BIGINT,
            rape_count BIGINT,
            robbery_count BIGINT,
            aggravated_assault_count BIGINT,
            burglary_count BIGINT,
            larceny_theft_count BIGINT,
            motor_vehicle_theft_count BIGINT,
            arson_count BIGINT,
            mapping_method TEXT,
            mapping_notes TEXT,
            source_file TEXT,
            loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS county_crime_annual (
            county_fips TEXT,
            county_name TEXT,
            state_code TEXT,
            state_name TEXT,
            year INTEGER,
            population_total BIGINT,
            total_crime_count BIGINT,
            violent_crime_count BIGINT,
            property_crime_count BIGINT,
            total_rate_per_1000 DOUBLE,
            violent_rate_per_1000 DOUBLE,
            property_rate_per_1000 DOUBLE,
            agency_count BIGINT,
            city_agency_count BIGINT,
            county_agency_count BIGINT,
            source_names TEXT,
            reporting_notes TEXT,
            created_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS zcta_county_crime_allocation (
            zcta TEXT,
            year INTEGER,
            county_fips TEXT,
            county_name TEXT,
            state_code TEXT,
            allocation_weight DOUBLE,
            zcta_population_total BIGINT,
            county_population_total BIGINT,
            county_total_rate_per_1000 DOUBLE,
            county_violent_rate_per_1000 DOUBLE,
            county_property_rate_per_1000 DOUBLE,
            allocated_total_crime_count DOUBLE,
            allocated_violent_crime_count DOUBLE,
            allocated_property_crime_count DOUBLE,
            source_names TEXT,
            created_at TIMESTAMP
        )
        """
    )
    aggregate_columns = """
            zcta TEXT,
            year INTEGER,
            population_total BIGINT,
            total_crime_count BIGINT,
            violent_crime_count BIGINT,
            property_crime_count BIGINT,
            drug_crime_count BIGINT,
            public_order_crime_count BIGINT,
            weapons_crime_count BIGINT,
            other_crime_count BIGINT,
            unknown_crime_count BIGINT,
            total_rate_per_1000 DOUBLE,
            violent_rate_per_1000 DOUBLE,
            property_rate_per_1000 DOUBLE,
            drug_rate_per_1000 DOUBLE,
            public_order_rate_per_1000 DOUBLE,
            weapons_rate_per_1000 DOUBLE,
            other_rate_per_1000 DOUBLE,
            unknown_rate_per_1000 DOUBLE,
            created_at TIMESTAMP
    """
    con.execute(f"CREATE TABLE IF NOT EXISTS zcta_crime_annual ({aggregate_columns})")
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS zcta_crime_monthly (
            month INTEGER,
            {aggregate_columns}
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS zcta_crime_index (
            zcta TEXT,
            year INTEGER,
            comparison_scope TEXT,
            comparison_scope_value TEXT,
            population_total BIGINT,
            total_crime_count BIGINT,
            violent_crime_count BIGINT,
            property_crime_count BIGINT,
            drug_crime_count BIGINT,
            public_order_crime_count BIGINT,
            weapons_crime_count BIGINT,
            other_crime_count BIGINT,
            unknown_crime_count BIGINT,
            total_rate_per_1000 DOUBLE,
            violent_rate_per_1000 DOUBLE,
            property_rate_per_1000 DOUBLE,
            drug_rate_per_1000 DOUBLE,
            public_order_rate_per_1000 DOUBLE,
            weapons_rate_per_1000 DOUBLE,
            other_rate_per_1000 DOUBLE,
            unknown_rate_per_1000 DOUBLE,
            total_rate_winsorized_per_1000 DOUBLE,
            violent_rate_winsorized_per_1000 DOUBLE,
            property_rate_winsorized_per_1000 DOUBLE,
            drug_rate_winsorized_per_1000 DOUBLE,
            public_order_rate_winsorized_per_1000 DOUBLE,
            weapons_rate_winsorized_per_1000 DOUBLE,
            other_rate_winsorized_per_1000 DOUBLE,
            total_crime_score_0_100 DOUBLE,
            violent_score_0_100 DOUBLE,
            property_score_0_100 DOUBLE,
            drug_score_0_100 DOUBLE,
            public_order_score_0_100 DOUBLE,
            weapons_score_0_100 DOUBLE,
            other_score_0_100 DOUBLE,
            overall_crime_score_0_100 DOUBLE,
            total_crime_percentile DOUBLE,
            violent_percentile DOUBLE,
            property_percentile DOUBLE,
            drug_percentile DOUBLE,
            public_order_percentile DOUBLE,
            weapons_percentile DOUBLE,
            other_percentile DOUBLE,
            overall_percentile DOUBLE,
            violent_z_score DOUBLE,
            property_z_score DOUBLE,
            drug_z_score DOUBLE,
            public_order_z_score DOUBLE,
            weapons_z_score DOUBLE,
            other_z_score DOUBLE,
            total_z_score DOUBLE,
            violent_index DOUBLE,
            property_index DOUBLE,
            total_index DOUBLE,
            composite_index DOUBLE,
            percentile_rank DOUBLE,
            data_coverage_score DOUBLE,
            confidence_grade TEXT,
            score_notes TEXT,
            violent_score_label TEXT,
            property_score_label TEXT,
            drug_score_label TEXT,
            public_order_score_label TEXT,
            weapons_score_label TEXT,
            other_score_label TEXT,
            total_crime_score_label TEXT,
            overall_crime_score_label TEXT,
            created_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT,
            command TEXT,
            config_snapshot_json TEXT,
            notes TEXT
        )
        """
    )
    _ensure_columns(
        con,
        "staged_crime_incidents",
        {
            "incident_count": "BIGINT",
        },
    )
    _ensure_columns(
        con,
        "normalized_crime_incidents",
        {
            "incident_count": "BIGINT",
        },
    )
    _ensure_columns(
        con,
        "zcta_crime_index",
        {
            "coverage_status": "TEXT",
            "data_source_type": "TEXT",
            "source_names": "TEXT",
            "source_count": "BIGINT",
            "assigned_incident_count": "BIGINT",
            "spatial_incident_count": "BIGINT",
            "is_modeled": "BOOLEAN",
            "observed_level": "TEXT",
            "county_fips": "TEXT",
            "county_name": "TEXT",
            "county_count": "BIGINT",
            "county_components": "TEXT",
            "allocation_method": "TEXT",
        },
    )


def _ensure_columns(con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str]) -> None:
    for column, column_type in columns.items():
        con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")
