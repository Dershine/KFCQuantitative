from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import CandidateScore, FactorBreakdown, RunStatus, SignalKind, SignalRun
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.scoring import ScoringService
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def prepare_morning(settings, at, codes):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}])
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    return database


def test_morning_watchlist_is_separate_and_never_creates_orders(settings):
    at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    database = prepare_morning(settings, at, codes)
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(pd.DataFrame()),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_morning(at)

    assert run.signal_kind == SignalKind.MORNING_WATCHLIST
    assert run.candidate_count == len(codes)
    assert not run.tradable
    assert database.proposed_orders(run.run_id).empty
    assert database.latest_signal_run(at.date(), SignalKind.MORNING_WATCHLIST.value)["run_id"] == run.run_id
    assert database.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value) is None


def test_morning_positive_news_is_capped_at_ten(settings):
    at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    events = pd.DataFrame(
        [
            {
                "event_id": f"positive-{index}",
                "ts_code": code,
                "direction": "positive",
                "severity": "low",
                "confidence": 1.0,
                "hard_block": False,
                "event_type": "positive_update",
                "evidence": "中标",
            }
            for index in range(10)
        ]
    )
    result = ScoringService(settings).score_morning(
        "morning", make_securities([(code, code)]), make_daily([code], at), at, events
    )
    assert result.candidates[0].factor_breakdown.news_score == 10.0


def test_preclose_continuity_is_small_and_does_not_limit_market(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    from tests.conftest import make_quotes

    result = ScoringService(settings).score(
        "preclose",
        make_securities([(code, code) for code in codes]),
        make_daily(codes, at),
        make_quotes(codes, at),
        at,
        morning_codes={codes[0]},
    )
    by_code = {candidate.ts_code: candidate for candidate in result.candidates}
    assert set(by_code) == set(codes)
    assert by_code[codes[0]].factor_breakdown.continuity_score in {0.0, 3.0}
    assert by_code[codes[1]].factor_breakdown.morning_status == "new"


def test_missing_minute_bars_are_not_counted_as_a_miss(settings):
    at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    database = Database(settings.database_path)
    database.initialize()
    run = SignalRun(
        as_of=at,
        signal_kind=SignalKind.MORNING_WATCHLIST,
        strategy_version="morning-v1",
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=False,
        candidate_count=1,
    )
    database.save_signal_run(run)
    database.save_candidates(
        [
            CandidateScore(
                run_id=run.run_id,
                ts_code="600000.SH",
                name="公司",
                rank=1,
                opportunity_score=80,
                factor_breakdown=FactorBreakdown(),
                quote_at=at,
            )
        ]
    )
    stored = database.latest_signal_run(at.date(), SignalKind.MORNING_WATCHLIST.value)
    outcomes = CandidateEvaluationService(database, settings, FakeLive(pd.DataFrame())).evaluate(
        stored, at.replace(hour=14, minute=40)
    )
    assert outcomes[0].status.value == "not_evaluable"
