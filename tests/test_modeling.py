import pandas as pd

from crime_index.modeling import build_modeled_annual_dataframe


def test_modeled_baseline_leaves_unmodeled_categories_null() -> None:
    population = pd.DataFrame(
        [
            {"zcta": "10001", "year": 2024, "population_total": 1000},
        ]
    )

    modeled = build_modeled_annual_dataframe(population, 4.0, 17.9, "national_modeled_baseline")

    assert modeled["violent_crime_count"].iloc[0] == 4
    assert modeled["property_crime_count"].iloc[0] == 18
    assert modeled["total_crime_count"].iloc[0] == 22
    assert pd.isna(modeled["weapons_crime_count"].iloc[0])
    assert modeled["comparison_scope"].iloc[0] == "national_modeled_baseline"
