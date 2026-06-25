import json
from pathlib import Path

from crime_index.static_bundle import build_static_bundle


def test_static_bundle_builds_manifest_and_yearly_scores(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    output_dir = tmp_path / "server"
    export_dir.mkdir()
    (export_dir / "zcta_crime_scores_2024.csv").write_text(
        "\n".join(
            [
                "zcta,year,comparison_scope,population_total,is_modeled,overall_crime_score_0_100,coverage_status,data_source_type",
                "601,2024,source_universe,1000,False,42.5,observed,observed",
            ]
        ),
        encoding="utf-8",
    )
    (export_dir / "zcta_national_coverage_2024.csv").write_text(
        "\n".join(
            [
                "zcta,year,population_total,coverage_status,data_source_type",
                "00601,2024,16669,national_modeled,modeled",
            ]
        ),
        encoding="utf-8",
    )
    (export_dir / "zcta_crime_scores_2024_national_modeled_baseline.csv").write_text(
        "\n".join(
            [
                "zcta,year,comparison_scope,population_total,is_modeled,overall_crime_score_0_100,coverage_status,data_source_type",
                "00601,2024,national_modeled_baseline,16669,True,50.0,national_modeled,modeled",
                "00602,2024,national_modeled_baseline,37083,True,50.0,national_modeled,modeled",
            ]
        ),
        encoding="utf-8",
    )

    manifest = build_static_bundle(
        export_dir,
        output_dir,
        years=[2024],
        allowed_origins=["http://localhost:5173", "https://fmr.fyi"],
    )

    assert manifest["years"]["2024"]["default_scope"] == "national_combined"
    assert manifest["cors"]["intended_consumer_origins"] == ["http://localhost:5173", "https://fmr.fyi"]
    assert (output_dir / ".nojekyll").exists()
    assert (output_dir / "crime-data-client.js").exists()
    client_js = (output_dir / "crime-data-client.js").read_text(encoding="utf-8")
    assert "getCrimeStatsForZips" in client_js
    assert "analyzeCrimeStatsGroup" in client_js

    scores = json.loads((output_dir / "2024" / "source_universe" / "scores.json").read_text(encoding="utf-8"))
    row = scores["records"][0]
    assert row["zcta"] == "00601"
    assert row["year"] == 2024
    assert row["population_total"] == 1000
    assert row["is_modeled"] is False
    assert row["overall_crime_score_0_100"] == 42.5

    coverage = json.loads((output_dir / "2024" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["row_count"] == 1

    combined = json.loads((output_dir / "2024" / "national_combined" / "scores.json").read_text(encoding="utf-8"))
    assert combined["row_count"] == 2
    assert combined["records"][0]["coverage_status"] == "observed"
    assert combined["records"][0]["comparison_scope"] == "national_combined"
    assert combined["records"][1]["coverage_status"] == "national_modeled"

    zip_api = manifest["years"]["2024"]["zip_api"]
    assert zip_api["path_template"] == "api/v1/2024/zips/{zip}.json"
    assert zip_api["scope"] == "national_combined"
    assert zip_api["row_count"] == 2

    zip_record = json.loads((output_dir / "api" / "v1" / "2024" / "zips" / "00601.json").read_text(encoding="utf-8"))
    assert zip_record["zip"] == "00601"
    assert zip_record["zcta"] == "00601"
    assert zip_record["api_version"] == "v1"
    assert zip_record["api_path"] == "/api/v1/2024/zips/00601.json"
    assert zip_record["coverage_status"] == "observed"
    assert zip_record["overall_crime_score_0_100"] == 42.5
