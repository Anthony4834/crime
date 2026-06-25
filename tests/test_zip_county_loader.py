import pandas as pd

from crime_index.ingest.zip_county_loader import normalize_county_key, normalize_zip_county_mapping


def test_zip_county_mapping_normalizes_equal_weights() -> None:
    raw = pd.DataFrame(
        [
            {
                "ZIP Code": "02481",
                "County Name": "Norfolk County",
                "State Code": "MA",
                "State Name": "Massachusetts",
                "County FIPS": "25021",
            },
            {
                "ZIP Code": "12345",
                "County Name": "Alpha County",
                "State Code": "NY",
                "State Name": "New York",
                "County FIPS": "36001",
            },
            {
                "ZIP Code": "12345",
                "County Name": "Beta County",
                "State Code": "NY",
                "State Name": "New York",
                "County FIPS": "36003",
            },
        ]
    )

    normalized = normalize_zip_county_mapping(raw, source="fixture")

    assert len(normalized) == 3
    assert normalized.loc[normalized["zcta"] == "02481", "allocation_weight"].iloc[0] == 1.0
    split = normalized[normalized["zcta"] == "12345"].sort_values("county_fips")
    assert split["allocation_weight"].round(3).tolist() == [0.5, 0.5]


def test_normalize_county_key_removes_common_suffixes() -> None:
    assert normalize_county_key("Norfolk County") == "norfolk"
    assert normalize_county_key("St. Louis City") == "saint louis"


def test_zip_county_mapping_uses_census_relationship_area_weights() -> None:
    raw = pd.DataFrame(
        [
            {
                "GEOID_ZCTA5_20": "02481",
                "GEOID_COUNTY_20": "25021",
                "NAMELSAD_COUNTY_20": "Norfolk County",
                "AREALAND_PART": "900",
                "AREAWATER_PART": "0",
            },
            {
                "GEOID_ZCTA5_20": "02481",
                "GEOID_COUNTY_20": "25017",
                "NAMELSAD_COUNTY_20": "Middlesex County",
                "AREALAND_PART": "100",
                "AREAWATER_PART": "0",
            },
        ]
    )

    normalized = normalize_zip_county_mapping(raw, source="census")

    split = normalized.sort_values("county_fips")
    assert split["allocation_weight"].round(3).tolist() == [0.1, 0.9]
