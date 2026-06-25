from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crime_index.config import load_sources

LOGGER = logging.getLogger(__name__)


def download_configured_sources(
    sources_config_path: str | Path = "config/sources.yaml",
    force: bool = False,
) -> dict[str, str]:
    sources = load_sources(sources_config_path)
    results: dict[str, str] = {}
    for source_name, source in sources.items():
        download_config = source.get("download") or {}
        if not download_config:
            results[source_name] = "no_download_config"
            continue
        destination = Path(source["file"])
        if destination.exists() and not force:
            results[source_name] = "exists"
            continue
        source_type = download_config.get("type")
        if source_type == "socrata_csv":
            url = build_socrata_csv_url(download_config)
        elif source_type == "direct":
            url = download_config["url"]
        elif source_type == "arcgis_query":
            destination.parent.mkdir(parents=True, exist_ok=True)
            row_count = download_arcgis_query(download_config, destination)
            results[source_name] = f"downloaded:{destination}:{row_count}_rows"
            continue
        elif source_type == "ckan_datastore":
            destination.parent.mkdir(parents=True, exist_ok=True)
            row_count = download_ckan_datastore(download_config, destination)
            results[source_name] = f"downloaded:{destination}:{row_count}_rows"
            continue
        else:
            results[source_name] = f"unsupported_download_type:{source_type}"
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Downloading %s from %s", source_name, url)
        request = Request(url, headers={"User-Agent": "crime-index-local-pipeline/0.1"})
        with urlopen(request, timeout=int(download_config.get("timeout_seconds", 300))) as response:
            destination.write_bytes(response.read())
        results[source_name] = f"downloaded:{destination}"
    return results


def build_socrata_csv_url(download_config: dict[str, Any]) -> str:
    base_url = str(download_config["endpoint"]).rstrip("/")
    if not base_url.endswith(".csv"):
        base_url = base_url + ".csv"
    params: dict[str, Any] = {}
    if download_config.get("limit"):
        params["$limit"] = download_config["limit"]
    if download_config.get("select"):
        params["$select"] = ",".join(download_config["select"])
    if download_config.get("where"):
        params["$where"] = download_config["where"]
    if download_config.get("order"):
        params["$order"] = download_config["order"]
    query = urlencode(params)
    return f"{base_url}?{query}" if query else base_url


def download_arcgis_query(download_config: dict[str, Any], destination: Path) -> int:
    layer_url = str(download_config["endpoint"]).rstrip("/")
    query_url = layer_url if layer_url.endswith("/query") else f"{layer_url}/query"
    out_fields = download_config.get("out_fields") or ["*"]
    if isinstance(out_fields, list):
        out_fields_param = ",".join(str(field) for field in out_fields)
    else:
        out_fields_param = str(out_fields)
    page_size = int(download_config.get("page_size", 2000))
    timeout = int(download_config.get("timeout_seconds", 300))
    where = str(download_config.get("where", "1=1"))
    return_geometry = bool(download_config.get("return_geometry", False))
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "f": "json",
            "where": where,
            "outFields": out_fields_param,
            "returnGeometry": str(return_geometry).lower(),
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        if download_config.get("out_sr"):
            params["outSR"] = download_config["out_sr"]
        if download_config.get("order_by_fields"):
            params["orderByFields"] = download_config["order_by_fields"]
        payload = _fetch_json(f"{query_url}?{urlencode(params)}", timeout)
        if payload.get("error"):
            raise RuntimeError(f"ArcGIS query failed: {payload['error']}")
        features = payload.get("features") or []
        if not features:
            break
        for feature in features:
            attributes = dict(feature.get("attributes") or {})
            geometry = feature.get("geometry") or {}
            if geometry:
                attributes.setdefault("geometry_x", geometry.get("x"))
                attributes.setdefault("geometry_y", geometry.get("y"))
                attributes.setdefault("geometry_lon", geometry.get("longitude"))
                attributes.setdefault("geometry_lat", geometry.get("latitude"))
            rows.append(attributes)
        if len(features) < page_size or not payload.get("exceededTransferLimit", len(features) == page_size):
            break
        offset += len(features)
    _write_csv(destination, rows)
    return len(rows)


def build_arcgis_query_url(download_config: dict[str, Any], offset: int = 0) -> str:
    layer_url = str(download_config["endpoint"]).rstrip("/")
    query_url = layer_url if layer_url.endswith("/query") else f"{layer_url}/query"
    out_fields = download_config.get("out_fields") or ["*"]
    if isinstance(out_fields, list):
        out_fields = ",".join(str(field) for field in out_fields)
    params: dict[str, Any] = {
        "f": "json",
        "where": download_config.get("where", "1=1"),
        "outFields": out_fields,
        "returnGeometry": str(bool(download_config.get("return_geometry", False))).lower(),
        "resultOffset": offset,
        "resultRecordCount": int(download_config.get("page_size", 2000)),
    }
    if download_config.get("out_sr"):
        params["outSR"] = download_config["out_sr"]
    if download_config.get("order_by_fields"):
        params["orderByFields"] = download_config["order_by_fields"]
    return f"{query_url}?{urlencode(params)}"


def download_ckan_datastore(download_config: dict[str, Any], destination: Path) -> int:
    page_size = int(download_config.get("page_size", 5000))
    timeout = int(download_config.get("timeout_seconds", 300))
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        url = build_ckan_datastore_sql_url(download_config, offset=offset)
        payload = _fetch_json(url, timeout)
        if not payload.get("success", False):
            raise RuntimeError(f"CKAN datastore query failed: {payload}")
        page = payload.get("result", {}).get("records") or []
        if not page:
            break
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    _write_csv(destination, rows)
    return len(rows)


def build_ckan_datastore_sql_url(download_config: dict[str, Any], offset: int = 0) -> str:
    base_url = str(download_config["base_url"]).rstrip("/")
    resource_id = str(download_config["resource_id"])
    fields = download_config.get("select") or ["*"]
    if isinstance(fields, list):
        quoted_fields = ", ".join(_quote_identifier(str(field)) for field in fields)
    else:
        quoted_fields = str(fields)
    sql = f"SELECT {quoted_fields} FROM {_quote_identifier(resource_id)}"
    if download_config.get("where"):
        sql += f" WHERE {download_config['where']}"
    if download_config.get("order_by"):
        sql += f" ORDER BY {download_config['order_by']}"
    sql += f" LIMIT {int(download_config.get('page_size', 5000))} OFFSET {offset}"
    return f"{base_url}/api/3/action/datastore_search_sql?{urlencode({'sql': sql})}"


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _fetch_json(url: str, timeout: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "crime-index-local-pipeline/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_csv(destination: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
