from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from kfcquant.config import get_settings
from kfcquant.db import Database


def test_dashboard_smoke_uses_query_projections_on_an_empty_database(tmp_path, monkeypatch):
    database_path = tmp_path / "dashboard.duckdb"
    monkeypatch.setenv("KFCQUANT_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("KFCQUANT_RAW_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("KFCQUANT_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("KFCQUANT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KFCQUANT_BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    settings = get_settings()
    Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
    ).initialize()

    dashboard = Path(__file__).resolve().parents[1] / "src" / "kfcquant" / "dashboard.py"
    app = AppTest.from_file(str(dashboard)).run(timeout=15)

    assert not app.exception
    assert len(app.tabs) == 7
    get_settings.cache_clear()
