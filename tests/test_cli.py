from pathlib import Path
from tempfile import TemporaryDirectory

from crime_index.cli import _clear_rebuild_outputs
from crime_index.db import get_connection, init_db


def test_clear_rebuild_outputs_preserves_pipeline_runs() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as temporary_dir:
        database_path = Path(temporary_dir) / "crime_index.duckdb"
        init_db(database_path)
        with get_connection(database_path) as con:
            con.execute("INSERT INTO zcta_national_coverage (zcta, year) VALUES ('00000', 2024)")
            con.execute("INSERT INTO zcta_crime_annual (zcta, year) VALUES ('00000', 2024)")
            con.execute("INSERT INTO pipeline_runs (run_id, status) VALUES ('run-1', 'completed')")

        _clear_rebuild_outputs(database_path)

        with get_connection(database_path) as con:
            assert con.execute("SELECT count(*) FROM zcta_national_coverage").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM zcta_crime_annual").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM pipeline_runs").fetchone()[0] == 1
