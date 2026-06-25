from __future__ import annotations

import re
import string


_PUNCT_TRANSLATION = str.maketrans({char: " " for char in string.punctuation})


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text.strip() or text.lower() in {"nan", "none", "null"}:
        return None
    return normalize_whitespace(text)


def normalize_for_match(value: object | None) -> str:
    cleaned = clean_text(value)
    if cleaned is None:
        return ""
    return normalize_whitespace(cleaned.lower().translate(_PUNCT_TRANSLATION))
