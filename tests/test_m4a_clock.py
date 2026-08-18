from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from kfcquant.clock import ReplayClock, SystemClock
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def test_system_and_replay_clocks_are_timezone_aware():
    system_now = SystemClock(SHANGHAI_TZ).now()
    replay_at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)

    assert system_now.tzinfo is not None
    assert system_now.utcoffset() is not None
    assert ReplayClock(replay_at).now() == replay_at
    with pytest.raises(ValueError, match="must not be None"):
        SystemClock(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(replay_at.replace(tzinfo=None))


def test_workflow_uses_injected_clock_for_default_use_case_and_audit_times(settings):
    replay_at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [
                {
                    "cal_date": replay_at.date(),
                    "is_open": True,
                    "pretrade_date": date(2026, 8, 7),
                }
            ]
        )
    )
    database.upsert_securities(make_securities([(codes[0], "公司")]))
    database.upsert_daily_bars(make_daily(codes, replay_at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(pd.DataFrame()),
        llm_provider=FakeLLM(),
        clock=ReplayClock(replay_at),
    )

    run = workflow.run_morning()

    assert run.as_of == replay_at
    job = database.latest_job("run-morning")
    assert job["started_at"] == replay_at
    assert job["finished_at"] == replay_at
    manifest = database.get_run_manifest(run.run_id)["manifest"]
    assert manifest.created_at == replay_at
    assert all(snapshot.captured_at == replay_at for snapshot in manifest.input_snapshots)
