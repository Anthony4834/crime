from crime_index.transform.rates import safe_rate


def test_safe_rate_per_1000() -> None:
    assert safe_rate(10, 1000) == 10


def test_safe_rate_zero_population_is_none() -> None:
    assert safe_rate(10, 0) is None


def test_safe_rate_missing_population_is_none() -> None:
    assert safe_rate(10, None) is None
