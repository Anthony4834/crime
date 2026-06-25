import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from crime_index.geo.zcta_assignment import assign_dataframe_to_zctas


def test_zcta_assignment_spatial_invalid_and_zip_fallback() -> None:
    zctas = gpd.GeoDataFrame(
        {
            "zcta": ["90210"],
            "state_fips": ["06"],
            "geometry": [Polygon([(-118.5, 34.0), (-118.0, 34.0), (-118.0, 34.5), (-118.5, 34.5)])],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    incidents = pd.DataFrame(
        [
            {"incident_id": "inside", "jurisdiction_state": "CA", "latitude": 34.2, "longitude": -118.2, "zcta_from_zip": None},
            {"incident_id": "bad-coord", "jurisdiction_state": "CA", "latitude": 0, "longitude": 0, "zcta_from_zip": "90210"},
            {"incident_id": "bad-zip", "jurisdiction_state": "CA", "latitude": 0, "longitude": 0, "zcta_from_zip": "99999"},
            {"incident_id": "missing", "jurisdiction_state": "CA", "latitude": None, "longitude": None, "zcta_from_zip": None},
            {"incident_id": "wrong-state", "jurisdiction_state": "CO", "latitude": 34.2, "longitude": -118.2, "zcta_from_zip": None},
        ]
    )

    assigned = assign_dataframe_to_zctas(incidents, zctas)
    by_id = assigned.set_index("incident_id")

    assert by_id.loc["inside", "zcta"] == "90210"
    assert by_id.loc["inside", "assignment_method"] == "spatial_join"
    assert by_id.loc["bad-coord", "zcta"] == "90210"
    assert by_id.loc["bad-coord", "assignment_method"] == "zip_fallback"
    assert pd.isna(by_id.loc["bad-zip", "zcta"])
    assert by_id.loc["bad-zip", "assignment_notes"] == "zip_fallback_without_loaded_zcta_geometry"
    assert pd.isna(by_id.loc["missing", "zcta"])
    assert by_id.loc["missing", "assignment_method"] == "unassigned"
    assert pd.isna(by_id.loc["wrong-state", "zcta"])
    assert by_id.loc["wrong-state", "assignment_notes"] == "valid_coordinates_outside_expected_state"


def test_zcta_assignment_rejects_out_of_state_zip_with_external_state_lookup() -> None:
    zctas = gpd.GeoDataFrame(
        {
            "zcta": ["90210", "89103"],
            "state_fips": [None, None],
            "geometry": [
                Polygon([(-118.5, 34.0), (-118.0, 34.0), (-118.0, 34.5), (-118.5, 34.5)]),
                Polygon([(-115.3, 36.0), (-115.0, 36.0), (-115.0, 36.3), (-115.3, 36.3)]),
            ],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    incidents = pd.DataFrame(
        [
            {"incident_id": "in-state-zip", "jurisdiction_state": "CA", "latitude": None, "longitude": None, "zcta_from_zip": "90210"},
            {"incident_id": "out-state-zip", "jurisdiction_state": "CA", "latitude": None, "longitude": None, "zcta_from_zip": "89103"},
        ]
    )

    assigned = assign_dataframe_to_zctas(
        incidents,
        zctas,
        zcta_state_fips={"90210": "06", "89103": "32"},
    )
    by_id = assigned.set_index("incident_id")

    assert by_id.loc["in-state-zip", "zcta"] == "90210"
    assert by_id.loc["in-state-zip", "assignment_method"] == "zip_fallback"
    assert pd.isna(by_id.loc["out-state-zip", "zcta"])
    assert by_id.loc["out-state-zip", "assignment_notes"] == "zip_fallback_outside_expected_state"
