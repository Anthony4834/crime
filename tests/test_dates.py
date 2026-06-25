from crime_index.normalize.dates import parse_occurred_at, parse_year_day_time


def test_parse_hhmm_time_column() -> None:
    parsed = parse_occurred_at("2023-01-01T00:00:00.000", "0845")
    assert parsed is not None
    assert parsed.year == 2023
    assert parsed.month == 1
    assert parsed.day == 1
    assert parsed.hour == 8
    assert parsed.minute == 45


def test_parse_year_day_time() -> None:
    parsed = parse_year_day_time("2024", "159", "20:00")
    assert parsed is not None
    assert parsed.year == 2024
    assert parsed.month == 6
    assert parsed.day == 7
    assert parsed.hour == 20
    assert parsed.minute == 0


def test_parse_arcgis_epoch_millis() -> None:
    parsed = parse_occurred_at(1712289600000)
    assert parsed is not None
    assert parsed.year == 2024
    assert parsed.month == 4
    assert parsed.day == 5


def test_parse_arcgis_epoch_millis_with_source_timezone() -> None:
    parsed = parse_occurred_at(1735690124000, timezone="America/New_York")
    assert parsed is not None
    assert parsed.year == 2024
    assert parsed.month == 12
    assert parsed.day == 31
    assert parsed.hour == 19
    assert parsed.minute == 8
