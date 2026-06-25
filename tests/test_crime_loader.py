import pandas as pd

from crime_index.ingest.crime_loader import build_staged_incidents


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
