from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from crime_index.config import load_offense_mapping
from crime_index.utils.text_utils import normalize_for_match


PRIORITY = ["violent", "weapons", "property", "drug", "public_order", "other"]


@dataclass(frozen=True)
class OffenseClassification:
    offense_normalized: str | None
    offense_group: str
    offense_subgroup: str


def classify_offense(
    offense_text: object | None,
    mapping: dict[str, Any] | None = None,
) -> OffenseClassification:
    mapping = mapping or load_offense_mapping()
    normalized = normalize_for_match(offense_text)
    if not normalized:
        return OffenseClassification(None, "unknown", "unknown")

    regex_matches = _match_regexes(normalized, mapping)
    if regex_matches:
        return _best_match(normalized, regex_matches)

    phrase_matches = _match_keywords(normalized, mapping, phrase_only=True)
    if phrase_matches:
        return _best_match(normalized, phrase_matches)

    single_word_matches = _match_keywords(normalized, mapping, phrase_only=False)
    if single_word_matches:
        return _best_match(normalized, single_word_matches)

    return OffenseClassification(normalized, "unknown", "unknown")


def _match_regexes(normalized: str, mapping: dict[str, Any]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for group, subgroups in mapping.items():
        if not isinstance(subgroups, dict):
            continue
        for subgroup, rules in subgroups.items():
            for pattern in (rules or {}).get("regexes", []) or []:
                if re.search(pattern, normalized):
                    matches.append((group, subgroup))
    return matches


def _match_keywords(normalized: str, mapping: dict[str, Any], phrase_only: bool) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for group, subgroups in mapping.items():
        if not isinstance(subgroups, dict):
            continue
        for subgroup, rules in subgroups.items():
            keywords = (rules or {}).get("keywords", []) or []
            normalized_keywords = sorted(
                (normalize_for_match(keyword) for keyword in keywords),
                key=len,
                reverse=True,
            )
            for keyword in normalized_keywords:
                if not keyword:
                    continue
                is_phrase = " " in keyword
                if phrase_only != is_phrase:
                    continue
                if is_phrase and keyword in normalized:
                    matches.append((group, subgroup))
                    break
                if not is_phrase and re.search(rf"\b{re.escape(keyword)}\b", normalized):
                    matches.append((group, subgroup))
                    break
    return matches


def _best_match(normalized: str, matches: list[tuple[str, str]]) -> OffenseClassification:
    for group in PRIORITY:
        for match_group, subgroup in matches:
            if match_group == group:
                return OffenseClassification(normalized, match_group, subgroup)
    group, subgroup = matches[0]
    return OffenseClassification(normalized, group, subgroup)
