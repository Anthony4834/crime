import pandas as pd

from crime_index.ingest.fbi_cde import _city_county_lookup
from crime_index.ingest.fbi_cde import city_key_from_agency_name
from crime_index.ingest.fbi_cde import normalize_agency_key
from crime_index.ingest.fbi_cde import normalize_place_key


def test_agency_name_normalization_supports_city_table_matching() -> None:
    assert city_key_from_agency_name("Beverly Police Department") == "beverly"
    assert city_key_from_agency_name("Auburn Department of Public Safety") == "auburn"
    assert normalize_place_key("Alexander City") == "alexander"
    assert normalize_agency_key("Norfolk County Sheriff's Office") == "norfolk county"


def test_city_lookup_resolves_multicounty_agency_to_largest_county() -> None:
    agencies = pd.DataFrame(
        [
            {
                "state_code": "OR",
                "agency_name": "Portland Police Department",
                "county_fips": "41005|41051|41067",
                "county_name": "Clackamas County|Multnomah County|Washington County",
            }
        ]
    )

    lookup = _city_county_lookup(agencies, {"41005": 445_621, "41051": 821_570, "41067": 570_999})

    assert lookup[("OR", "portland")][0:2] == ("41051", "Multnomah County")
    assert lookup[("OR", "portland")][3] == "multi_county_agency:41005|41051|41067;primary_county_by_population"
