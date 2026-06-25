import pandas as pd

from crime_index.transform.aggregate import build_annual_aggregates


def test_annual_aggregates_sum_incident_count() -> None:
    incidents = pd.DataFrame(
        [
            {
                "incident_id": "a",
                "incident_count": 4,
                "occurred_year": 2024,
                "offense_group": "violent",
                "zcta": "55401",
            },
            {
                "incident_id": "b",
                "incident_count": 1,
                "occurred_year": 2024,
                "offense_group": "property",
                "zcta": "55401",
            },
        ]
    )
    population = pd.DataFrame([{"zcta": "55401", "year": 2024, "population_total": 1000}])

    annual = build_annual_aggregates(incidents, population, 2024)
    row = annual.iloc[0]

    assert row["violent_crime_count"] == 4
    assert row["property_crime_count"] == 1
    assert row["total_crime_count"] == 5
    assert row["total_rate_per_1000"] == 5
