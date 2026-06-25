from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")
DEFAULT_SOURCES_PATH = Path("config/sources.yaml")
DEFAULT_OFFENSE_MAPPING_PATH = Path("config/offense_mapping.yaml")


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def load_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> dict[str, Any]:
    return load_yaml(path)


def load_sources(path: str | Path = DEFAULT_SOURCES_PATH) -> dict[str, Any]:
    data = load_yaml(path)
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("config/sources.yaml must contain a 'sources' mapping")
    return sources


def select_sources(sources: dict[str, Any], source_names: list[str] | tuple[str, ...] | None) -> dict[str, Any]:
    if not source_names:
        return sources
    missing = [source_name for source_name in source_names if source_name not in sources]
    if missing:
        available = ", ".join(sorted(sources))
        raise ValueError(f"Unknown source(s): {', '.join(missing)}. Available sources: {available}")
    return {source_name: sources[source_name] for source_name in source_names}


def load_offense_mapping(path: str | Path = DEFAULT_OFFENSE_MAPPING_PATH) -> dict[str, Any]:
    return load_yaml(path)


def get_database_path(settings: dict[str, Any] | None = None) -> Path:
    env_path = os.getenv("CRIME_INDEX_DATABASE_PATH")
    if env_path:
        return Path(env_path)
    settings = settings or load_settings()
    return Path(settings.get("database", {}).get("path", "db/crime_index.duckdb"))


def get_nested(config: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    value: Any = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value
