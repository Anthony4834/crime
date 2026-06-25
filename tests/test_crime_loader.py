import pandas as pd

from crime_index.db import get_connection
from crime_index.ingest.crime_loader import apply_row_filters
from crime_index.ingest.crime_loader import build_staged_incidents
from crime_index.ingest.crime_loader import ingest_crime
from crime_index.ingest.crime_loader import read_tabular_file


def test_read_tabular_file_supports_configured_csv_options(tmp_path) -> None:
    source = tmp_path / "incidents.csv"
    source.write_text("zip,offense\n00123,THEFT\nbad,row,extra\n", encoding="utf-8")

    df = read_tabular_file(source, {"dtype": "str", "on_bad_lines": "skip"})

    assert df.to_dict("records") == [{"zip": "00123", "offense": "THEFT"}]


def test_apply_row_filters_supports_include_and_exclude_rules() -> None:
    df = pd.DataFrame(
        [
            {"year": "2024", "code": "THEFT", "description": "Bike Theft"},
            {"year": "2024", "code": "NONCRIM", "description": "Welfare Check"},
            {"year": "2024", "code": "BURGLARY", "description": "Residential Burglary"},
            {"year": "2023", "code": "ROBBERY", "description": "Robbery"},
        ]
    )

    filtered = apply_row_filters(
        df,
        {
            "include": [{"column": "year", "values": ["2024"]}],
            "exclude": [
                {"column": "code", "values": ["noncrim"]},
                {"column": "description", "regex": "welfare|traffic"},
            ],
        },
    )

    assert filtered.to_dict("records") == [
        {"year": "2024", "code": "THEFT", "description": "Bike Theft"},
        {"year": "2024", "code": "BURGLARY", "description": "Residential Burglary"},
    ]


def test_build_staged_incidents_supports_day_of_year_and_point_wkt() -> None:
    df = pd.DataFrame(
        [
            {
                "id": "1",
                "year1": "2024",
                "date1dayofyear": "159",
                "time1": "20:00",
                "offense": "BURGLARY",
                "point": "POINT (-96.88994197 32.660977013)",
            }
        ]
    )
    staged = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "year_column": "year1",
            "day_of_year_column": "date1dayofyear",
            "time_column": "time1",
            "offense_column": "offense",
            "geocoded_point_column": "point",
        },
    )

    row = staged.iloc[0]
    assert row["occurred_date"].isoformat() == "2024-06-07"
    assert row["latitude_raw"] == 32.660977013
    assert row["longitude_raw"] == -96.88994197


def test_build_staged_incidents_supports_parenthesized_lat_lon_points() -> None:
    df = pd.DataFrame(
        [
            {
                "id": "1",
                "date": "2024-03-02T13:47:00",
                "offense": "THEFT",
                "point": "(32.637238995307605, -97.393977584559693)",
            }
        ]
    )
    staged = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "date_column": "date",
            "offense_column": "offense",
            "geocoded_point_column": "point",
        },
    )

    row = staged.iloc[0]
    assert row["latitude_raw"] == 32.637238995307605
    assert row["longitude_raw"] == -97.393977584559693


def test_build_staged_incidents_uses_offense_fallback_columns() -> None:
    df = pd.DataFrame(
        [
            {
                "id": "1",
                "date": "2024-01-01T12:00:00",
                "description": None,
                "offense": "Miscellaneous Investigation",
            }
        ]
    )
    staged = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "date_column": "date",
            "offense_column": "description",
            "offense_fallback_columns": ["offense"],
        },
    )

    assert staged.iloc[0]["offense_raw"] == "Miscellaneous Investigation"


def test_build_staged_incidents_supports_count_column() -> None:
    df = pd.DataFrame(
        [
            {
                "id": "1",
                "date": "2024-01-01T12:00:00",
                "offense": "Simple Assault",
                "crime_count": "4",
            }
        ]
    )
    staged = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "date_column": "date",
            "offense_column": "offense",
            "count_column": "crime_count",
        },
    )

    assert staged.iloc[0]["incident_count"] == 4


def test_build_staged_incidents_only_timezone_shifts_epoch_when_configured() -> None:
    df = pd.DataFrame(
        [
            {
                "id": "1",
                "date": 1735690124000,
                "offense": "THEFT",
            }
        ]
    )

    unshifted = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "date_column": "date",
            "offense_column": "offense",
            "timezone": "America/New_York",
        },
    )
    shifted = build_staged_incidents(
        df,
        "test",
        {
            "incident_id_column": "id",
            "date_column": "date",
            "offense_column": "offense",
            "epoch_timezone": "America/New_York",
        },
    )

    assert unshifted.iloc[0]["occurred_date"].isoformat() == "2025-01-01"
    assert shifted.iloc[0]["occurred_date"].isoformat() == "2024-12-31"


def test_ingest_crime_can_replace_one_source(tmp_path) -> None:
    source_a = tmp_path / "a.csv"
    source_b = tmp_path / "b.csv"
    source_a.write_text("id,date,offense\n1,2024-01-01,THEFT\n", encoding="utf-8")
    source_b.write_text("id,date,offense\n1,2024-01-01,BURGLARY\n", encoding="utf-8")
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"""
sources:
  a:
    file: {source_a.as_posix()}
    incident_id_column: id
    date_column: date
    offense_column: offense
  b:
    file: {source_b.as_posix()}
    incident_id_column: id
    date_column: date
    offense_column: offense
""",
        encoding="utf-8",
    )
    database = tmp_path / "crime_index.duckdb"

    ingest_crime(config, database_path=database)
    source_b.write_text(
        "id,date,offense\n1,2024-01-01,BURGLARY\n2,2024-01-02,ROBBERY\n",
        encoding="utf-8",
    )

    ingest_crime(config, database_path=database, source_names=["b"])

    with get_connection(database) as con:
        rows = dict(
            con.execute(
                """
                SELECT source_name, count(*)
                FROM staged_crime_incidents
                GROUP BY source_name
                ORDER BY source_name
                """
            ).fetchall()
        )

    assert rows == {"a": 1, "b": 2}
