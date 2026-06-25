from __future__ import annotations

import pandas as pd


CATEGORIES = ["violent", "property", "drug", "public_order", "weapons", "other", "unknown"]


def safe_rate(count: object, population: object, multiplier: float = 1000.0) -> float | None:
    if pd.isna(count) or pd.isna(population):
        return None
    population_value = float(population)
    if population_value <= 0:
        return None
    return float(count) / population_value * multiplier


def add_rate_columns(df: pd.DataFrame, rate_multiplier: float = 1000.0) -> pd.DataFrame:
    output = df.copy()
    output["total_rate_per_1000"] = [
        safe_rate(count, population, rate_multiplier)
        for count, population in zip(output["total_crime_count"], output["population_total"])
    ]
    for category in CATEGORIES:
        output[f"{category}_rate_per_1000"] = [
            safe_rate(count, population, rate_multiplier)
            for count, population in zip(output[f"{category}_crime_count"], output["population_total"])
        ]
    return output
