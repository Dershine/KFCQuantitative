from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from kfcquant.config import SHANGHAI_TZ, get_settings
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


def test_dashboard_surfaces_failed_preclose_job_instead_of_silent_empty_state(tmp_path, monkeypatch):
    database_path = tmp_path / "dashboard-failed.duckdb"
    monkeypatch.setenv("KFCQUANT_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("KFCQUANT_RAW_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("KFCQUANT_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("KFCQUANT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KFCQUANT_BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    settings = get_settings()
    database = Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
    )
    database.initialize()
    started = datetime.now(SHANGHAI_TZ).replace(hour=14, minute=40, second=0, microsecond=0)
    database.start_job("failed-preclose", "run-preclose", started, timedelta(minutes=15))
    database.finish_job(
        "failed-preclose",
        started + timedelta(minutes=2),
        "failed",
        "live_quote contains data after information_cutoff",
    )

    dashboard = Path(__file__).resolve().parents[1] / "src" / "kfcquant" / "dashboard.py"
    app = AppTest.from_file(str(dashboard)).run(timeout=15)

    assert not app.exception
    assert any("运行失败" in error.value for error in app.error)
    assert any("live_quote contains data after information_cutoff" in error.value for error in app.error)
    get_settings.cache_clear()
