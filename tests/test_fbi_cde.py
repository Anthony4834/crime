from crime_index.ingest.fbi_cde import city_key_from_agency_name, normalize_agency_key, normalize_place_key


def test_agency_name_normalization_supports_city_table_matching() -> None:
    assert city_key_from_agency_name("Beverly Police Department") == "beverly"
    assert city_key_from_agency_name("Auburn Department of Public Safety") == "auburn"
    assert normalize_place_key("Alexander City") == "alexander"
    assert normalize_agency_key("Norfolk County Sheriff's Office") == "norfolk county"
