from crime_index.ingest.source_downloader import (
    build_arcgis_query_url,
    build_ckan_datastore_sql_url,
    build_socrata_csv_url,
    download_configured_sources,
)


def test_build_socrata_csv_url() -> None:
    url = build_socrata_csv_url(
        {
            "endpoint": "https://example.test/resource/abcd-1234",
            "limit": 10,
            "select": ["id", "date", "description"],
            "where": "date between '2024-01-01' and '2024-12-31'",
            "order": "date ASC",
        }
    )

    assert url.startswith("https://example.test/resource/abcd-1234.csv?")
    assert "%24limit=10" in url
    assert "%24select=id%2Cdate%2Cdescription" in url
    assert "date+between" in url
    assert "%24order=date+ASC" in url


def test_build_arcgis_query_url() -> None:
    url = build_arcgis_query_url(
        {
            "endpoint": "https://example.test/FeatureServer/0",
            "where": "occurred >= DATE '2024-01-01'",
            "out_fields": ["id", "occurred", "category"],
            "page_size": 500,
            "return_geometry": True,
            "out_sr": 4326,
        },
        offset=1000,
    )

    assert url.startswith("https://example.test/FeatureServer/0/query?")
    assert "outFields=id%2Coccurred%2Ccategory" in url
    assert "resultOffset=1000" in url
    assert "resultRecordCount=500" in url
    assert "returnGeometry=true" in url
    assert "outSR=4326" in url


def test_build_ckan_datastore_sql_url() -> None:
    url = build_ckan_datastore_sql_url(
        {
            "base_url": "https://data.example.test",
            "resource_id": "abcd-1234",
            "select": ["id", "occurred", "offense"],
            "where": "\"YEAR\" = '2024'",
            "order_by": "\"id\"",
            "page_size": 1000,
        },
        offset=2000,
    )

    assert url.startswith("https://data.example.test/api/3/action/datastore_search_sql?")
    assert "SELECT+%22id%22%2C+%22occurred%22%2C+%22offense%22+FROM+%22abcd-1234%22" in url
    assert "WHERE+%22YEAR%22+%3D+%272024%27" in url
    assert "LIMIT+1000+OFFSET+2000" in url


def test_download_configured_sources_can_filter_by_source(tmp_path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
sources:
  first:
    file: first.csv
  second:
    file: second.csv
""",
        encoding="utf-8",
    )

    results = download_configured_sources(config, source_names=["second"])

    assert results == {"second": "no_download_config"}
