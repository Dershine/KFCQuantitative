from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.db import MIGRATIONS, Database
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def test_custom_policy_flows_from_schedule_through_run_selection_and_orders(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "stage.duckdb",
        raw_data_dir=tmp_path / "raw",
        report_dir=tmp_path / "reports",
        runtime_dir=tmp_path / "runtime",
        backup_dir=tmp_path / "backups",
        max_positions=2,
        position_fraction=0.5,
        selection={"top_n": 2, "candidate_limit": 3},
        schedule={
            "morning_evaluation_at": "14:25",
            "preclose_run_at": "14:30",
            "preclose_window_start": "14:25",
            "preclose_window_end": "14:33",
            "fill_at": "14:35",
            "fill_window_start": "14:33",
            "fill_window_end": "14:40",
        },
    )
    at = datetime(2026, 8, 10, 14, 30, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "601001.SH", "603001.SH", "605001.SH"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(at)

    assert settings.schedule.registration_plan()["tasks"][3]["at"] == "14:30"
    assert run.tradable
    assert run.candidate_count == 3
    assert len(database.get_candidates(run.run_id)) == 3
    assert len(database.proposed_orders(run.run_id)) == 2
    assert database.migration_version() == len(MIGRATIONS)
