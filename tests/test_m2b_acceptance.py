from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import SignalKind
from kfcquant.services.workflow import Workflow
from kfcquant.strategy import build_default_strategy_registry
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def test_m2b_two_signal_end_to_end_preserves_candidate_results_and_order_safety(settings):
    morning_at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    preclose_at = morning_at.replace(hour=14, minute=40)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [
                {
                    "cal_date": morning_at.date(),
                    "is_open": True,
                    "pretrade_date": (morning_at - timedelta(days=3)).date(),
                }
            ]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, morning_at))
    registry = build_default_strategy_registry(settings)
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, preclose_at)),
        llm_provider=FakeLLM(),
        strategy_registry=registry,
    )

    morning = workflow.run_morning(morning_at)
    preclose = workflow.run_preclose(preclose_at)
    morning_candidates = database.get_candidates(morning.run_id).sort_values("rank")
    preclose_candidates = database.get_candidates(preclose.run_id).sort_values("rank")

    assert morning_candidates[["ts_code", "rank", "opportunity_score"]].to_dict("records") == [
        {"ts_code": "002001.SZ", "rank": 1, "opportunity_score": 67.5},
        {"ts_code": "000001.SZ", "rank": 2, "opportunity_score": 49.5},
        {"ts_code": "600000.SH", "rank": 3, "opportunity_score": 31.5},
    ]
    assert preclose_candidates[["ts_code", "rank", "opportunity_score"]].to_dict("records") == [
        {"ts_code": "002001.SZ", "rank": 1, "opportunity_score": 84.0},
        {"ts_code": "000001.SZ", "rank": 2, "opportunity_score": 56.4},
        {"ts_code": "600000.SH", "rank": 3, "opportunity_score": 28.8},
    ]
    assert morning.signal_kind == SignalKind.MORNING_WATCHLIST
    assert not morning.tradable
    assert database.proposed_orders(morning.run_id).empty
    assert preclose.tradable
    assert set(database.proposed_orders(preclose.run_id)["ts_code"]) == set(codes)
