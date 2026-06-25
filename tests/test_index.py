import duckdb
import pandas as pd

from crime_index.config import load_settings
from crime_index.transform.index import build_index_dataframe, calculate_coverage_metrics


def test_index_mean_is_roughly_100_and_higher_rates_score_higher() -> None:
    annual = pd.DataFrame(
        [
            _row("10001", 1000, violent=1, property=2, total=3),
            _row("10002", 1000, violent=3, property=4, total=7),
            _row("10003", 1000, violent=5, property=8, total=13),
        ]
    )
    annual["comparison_scope"] = "source_universe"
    annual["comparison_scope_value"] = ""

    indexed = build_index_dataframe(annual, load_settings())

    assert round(indexed["total_index"].mean(), 6) == 100
    low = indexed[indexed["zcta"] == "10001"].iloc[0]
    high = indexed[indexed["zcta"] == "10003"].iloc[0]
    assert high["overall_crime_score_0_100"] > low["overall_crime_score_0_100"]


def test_null_rates_do_not_crash_index() -> None:
    annual = pd.DataFrame(
        [
            _row("10001", None, violent=1, property=2, total=3),
            _row("10002", 1000, violent=0, property=0, total=0),
        ]
    )
    annual["comparison_scope"] = "source_universe"
    annual["comparison_scope_value"] = ""

    indexed = build_index_dataframe(annual, load_settings())

    assert len(indexed) == 2
    assert pd.isna(indexed[indexed["zcta"] == "10001"]["total_crime_score_0_100"].iloc[0])


def test_coverage_metrics_use_incident_count_weights() -> None:
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE normalized_crime_incidents (
            incident_id TEXT,
            occurred_date DATE,
            occurred_year INTEGER,
            offense_group TEXT,
            source_name TEXT,
            incident_count BIGINT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE incident_zcta_assignment (
            incident_id TEXT,
            zcta TEXT,
            assignment_method TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO normalized_crime_incidents VALUES
          ('a', '2024-01-01', 2024, 'violent', 'weighted_source', 4),
          ('b', NULL, 2024, 'unknown', 'weighted_source', 1)
        """
    )
    con.execute(
        """
        INSERT INTO incident_zcta_assignment VALUES
          ('a', '55401', 'spatial_join'),
          ('b', NULL, 'unassigned')
        """
    )

    metrics = calculate_coverage_metrics(con, 2024)
    by_zcta = metrics["by_zcta"].iloc[0]

    assert metrics["zcta_assignment_rate"] == 0.8
    assert metrics["spatial_assignment_rate"] == 0.8
    assert metrics["offense_classification_rate"] == 0.8
    assert metrics["date_completeness_rate"] == 0.8
    assert by_zcta["assigned_count"] == 4
    assert by_zcta["spatial_count"] == 4


def _row(zcta: str, population: int | None, violent: int, property: int, total: int) -> dict[str, object]:
    return {
        "zcta": zcta,
        "year": 2023,
        "population_total": population,
        "total_crime_count": total,
        "violent_crime_count": violent,
        "property_crime_count": property,
        "drug_crime_count": 0,
        "public_order_crime_count": 0,
        "weapons_crime_count": 0,
        "other_crime_count": 0,
        "unknown_crime_count": 0,
    }
