from crime_index.config import load_offense_mapping
from crime_index.normalize.offense_classifier import classify_offense


MAPPING = load_offense_mapping()


def test_robbery_classifies_as_violent() -> None:
    result = classify_offense("ROBBERY - STREET", MAPPING)
    assert result.offense_group == "violent"
    assert result.offense_subgroup == "robbery"


def test_burglary_classifies_as_property() -> None:
    result = classify_offense("BURGLARY RESIDENCE", MAPPING)
    assert result.offense_group == "property"
    assert result.offense_subgroup == "burglary"


def test_auto_theft_classifies_as_motor_vehicle_theft() -> None:
    result = classify_offense("AUTO THEFT", MAPPING)
    assert result.offense_group == "property"
    assert result.offense_subgroup == "motor_vehicle_theft"


def test_narcotics_classifies_as_drug() -> None:
    result = classify_offense("NARCOTICS POSSESSION", MAPPING)
    assert result.offense_group == "drug"
    assert result.offense_subgroup == "drug_offense"


def test_unknown_classifies_as_unknown() -> None:
    result = classify_offense("SUSPICIOUS INCIDENT", MAPPING)
    assert result.offense_group == "unknown"
    assert result.offense_subgroup == "unknown"
