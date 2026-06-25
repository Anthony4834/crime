from __future__ import annotations

from pathlib import Path

from crime_index.db import get_connection, init_db
from crime_index.quality.checks import build_quality_report, write_quality_report


def profile(
    year: int | None = None,
    database_path: str | Path | None = None,
    output_dir: str | Path = "data/processed",
) -> dict[str, object]:
    init_db(database_path)
    with get_connection(database_path) as con:
        report = build_quality_report(con, year)
    written = write_quality_report(report, output_dir)
    return {"report": report, "files": written}
