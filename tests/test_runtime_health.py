from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.observability import MemoryObservabilitySink, Observability
from kfcquant.runtime import health_info


def test_health_separates_infrastructure_from_current_research_session_failure(settings):
    now = datetime(2026, 8, 21, 14, 48, tzinfo=SHANGHAI_TZ)
    database = Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
    )
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": date(2026, 8, 21), "is_open": True, "pretrade_date": date(2026, 8, 20)}]
        )
    )
    started = now.replace(hour=14, minute=40)
    database.start_job("failed-preclose", "run-preclose", started, timedelta(minutes=15))
    database.finish_job(
        "failed-preclose",
        started + timedelta(minutes=2),
        "failed",
        "live_quote contains data after information_cutoff",
    )
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    (settings.runtime_dir / "worker-heartbeat.json").write_text(
        json.dumps({"at": now.isoformat()}),
        encoding="utf-8",
    )

    payload = health_info(
        settings,
        Observability((MemoryObservabilitySink(),)),
        now=now,
    )

    assert payload["status"] == "ok"
    assert payload["research"]["status"] == "degraded"
    assert payload["research"]["trading_day"] is True
    failed = payload["research"]["unhealthy_jobs"]["run-preclose"]
    assert failed["job_run_id"] == "failed-preclose"
    assert failed["status"] == "failed"


def test_health_reports_missing_signal_jobs_after_their_deadlines(settings):
    now = datetime(2026, 8, 21, 14, 48, tzinfo=SHANGHAI_TZ)
    database = Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
    )
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": date(2026, 8, 21), "is_open": True, "pretrade_date": date(2026, 8, 20)}]
        )
    )
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    (settings.runtime_dir / "worker-heartbeat.json").write_text(
        json.dumps({"at": now.isoformat()}),
        encoding="utf-8",
    )

    payload = health_info(settings, Observability((MemoryObservabilitySink(),)), now=now)

    assert payload["research"]["status"] == "degraded"
    assert payload["research"]["unhealthy_jobs"]["run-morning"]["status"] == "missing"
    assert payload["research"]["unhealthy_jobs"]["run-preclose"]["status"] == "missing"
