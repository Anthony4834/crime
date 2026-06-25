from __future__ import annotations

import math
import re
from typing import Any


def clean_zip(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    match = re.search(r"(\d{5})", text)
    if not match:
        return None
    return match.group(1)


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def is_valid_lat_lon(latitude: Any, longitude: Any, invalid_values: list[list[float]] | None = None) -> bool:
    lat = to_float(latitude)
    lon = to_float(longitude)
    if lat is None or lon is None:
        return False
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return False
    invalid_values = invalid_values or [[0, 0]]
    for invalid_lat, invalid_lon in invalid_values:
        if lat == invalid_lat and lon == invalid_lon:
            return False
    return True


def point_wkt(latitude: Any, longitude: Any) -> str | None:
    if not is_valid_lat_lon(latitude, longitude):
        return None
    lat = to_float(latitude)
    lon = to_float(longitude)
    return f"POINT ({lon} {lat})"
