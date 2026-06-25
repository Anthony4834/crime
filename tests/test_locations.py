from crime_index.normalize.locations import clean_zip


def test_clean_zip_extracts_five_digits() -> None:
    assert clean_zip("90210-1234") == "90210"
    assert clean_zip("ZCTA5 90210") == "90210"
    assert clean_zip("860Z200US90210") == "90210"


def test_clean_zip_invalid_strings_return_none() -> None:
    assert clean_zip("not a zip") is None
    assert clean_zip("") is None
    assert clean_zip(None) is None
