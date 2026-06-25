from crime_index.config import load_offense_mapping
from crime_index.normalize.offense_classifier import classify_offense
import pytest


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


@pytest.mark.parametrize(
    ("offense", "expected_group"),
    [
        ("HSC 481.121(B)(1) Poss Marij <=2OZ", "drug"),
        ("PC 28.03(B)(2) Crim Misc>=$100<$750", "property"),
        ("PC 49.04 Driving While Intoxicated", "public_order"),
        ("PC 25.07 G Violation of Bond/Protective Order", "public_order"),
        ("PC 22.07(A)(1&2) Terroristic Threat", "violent"),
        ("PC 38.04(b)(2)(A) Evading Arrest Detention w/vehic", "public_order"),
        ("Damage of Property - Substantial Damage", "property"),
        ("Fraudulent Use of Credit Cards", "property"),
        ("Order of Protection Violation - Dv", "public_order"),
        ("Public Sexual Indecency", "public_order"),
        ("Prohibited Possesser", "weapons"),
        ("Child/Adult Abuse-Sr Inj-Dv", "violent"),
        ("LOCAL Local Class C Warrants", "public_order"),
        ("PC 42.0622 Interfer W/Emergency Call", "public_order"),
    ],
)
def test_texas_penal_code_phrases_classify(offense: str, expected_group: str) -> None:
    result = classify_offense(offense, MAPPING)
    assert result.offense_group == expected_group
