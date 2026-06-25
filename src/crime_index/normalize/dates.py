from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import re


def parse_occurred_at(
    date_value: object | None,
    time_value: object | None = None,
    timezone: str | None = None,
) -> pd.Timestamp | None:
    if date_value is None or pd.isna(date_value):
        return None
    epoch_ms = _parse_epoch_millis(date_value)
    if epoch_ms is not None:
        parsed_epoch = pd.to_datetime(epoch_ms, unit="ms", utc=timezone is not None, errors="coerce")
        if not pd.isna(parsed_epoch):
            if timezone:
                parsed_epoch = parsed_epoch.tz_convert(timezone).tz_localize(None)
            return parsed_epoch
    date_text = str(date_value).strip()
    if not date_text or date_text.lower() in {"nan", "none", "null"}:
        return None
    if time_value is not None and not pd.isna(time_value):
        time_text = str(time_value).strip()
        if time_text and time_text.lower() not in {"nan", "none", "null"}:
            if re.fullmatch(r"\d{1,4}", time_text):
                padded = time_text.zfill(4)
                hour = int(padded[:2])
                minute = int(padded[2:])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    time_text = f"{hour:02d}:{minute:02d}"
                    date_text = date_text.split("T")[0].split(" ")[0]
            date_text = f"{date_text} {time_text}"
    parsed = pd.to_datetime(date_text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed


def _parse_epoch_millis(value: object) -> int | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < 10_000_000_000:
        return None
    return int(number)


def parse_year_day_time(
    year_value: object | None,
    day_of_year_value: object | None,
    time_value: object | None = None,
) -> pd.Timestamp | None:
    if year_value is None or day_of_year_value is None or pd.isna(year_value) or pd.isna(day_of_year_value):
        return None
    try:
        year = int(float(str(year_value).strip()))
        day_of_year = int(float(str(day_of_year_value).strip()))
    except (TypeError, ValueError):
        return None
    if day_of_year < 1 or day_of_year > 366:
        return None
    base = datetime(year, 1, 1) + timedelta(days=day_of_year - 1)
    time_text = "" if time_value is None or pd.isna(time_value) else str(time_value).strip()
    if time_text and time_text.lower() not in {"nan", "none", "null"}:
        parsed_with_time = parse_occurred_at(base.date().isoformat(), time_text)
        if parsed_with_time is not None:
            return parsed_with_time
    return pd.Timestamp(base)
