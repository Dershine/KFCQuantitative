from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import RiskEvent, SignalKind
from kfcquant.services.workflow import Workflow
from kfcquant.strategy import StrategyContext, StrategyIdentity, StrategyRegistry, build_default_strategy_registry
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


class RecordingStrategy:
    def __init__(self, delegate, identity: StrategyIdentity):
        self.delegate = delegate
        self.signal_kind = delegate.signal_kind
        self.identity = identity
        self.requirements = delegate.requirements
        self.contexts: list[StrategyContext] = []

    def evaluate(self, context: StrategyContext):
        self.contexts.append(context)
        return self.delegate.evaluate(context)


def test_m2c_shared_selection_semantics_cross_workflow_portfolio_and_evaluation(settings):
    configured = settings.model_copy(
        update={
            "max_positions": 2,
            "position_fraction": 0.5,
            "selection": settings.selection.model_copy(
                update={"top_n": 2, "candidate_limit": 3, "minimum_opportunity_score": 50.0}
            ),
        }
    )
    morning_at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    preclose_at = morning_at.replace(hour=14, minute=40)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    database = Database(configured.database_path, configured.initial_cash)
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

    defaults = build_default_strategy_registry(configured)
    morning = RecordingStrategy(
        defaults.resolve(SignalKind.MORNING_WATCHLIST), StrategyIdentity("m2c-morning", "m2c-morning-v1")
    )
    preclose = RecordingStrategy(
        defaults.resolve(SignalKind.PRECLOSE_ENTRY), StrategyIdentity("m2c-preclose", "m2c-preclose-v1")
    )
    workflow = Workflow(
        configured,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, preclose_at)),
        llm_provider=FakeLLM(),
        strategy_registry=StrategyRegistry([morning, preclose]),
    )

    morning_run = workflow.run_morning(morning_at)
    preclose_run = workflow.run_preclose(preclose_at)

    morning_candidates = database.get_candidates(morning_run.run_id)
    preclose_candidates = database.get_candidates(preclose_run.run_id)
    orders = database.proposed_orders(preclose_run.run_id)
    outcomes = workflow.evaluation.evaluate(
        database.latest_signal_run(preclose_at.date(), SignalKind.PRECLOSE_ENTRY.value),
        preclose_at + timedelta(days=1),
    )

    assert morning_candidates["ts_code"].tolist() == ["002001.SZ"]
    assert preclose.contexts[0].previous_signal_codes == frozenset({"002001.SZ"})
    assert preclose_candidates["ts_code"].tolist() == ["002001.SZ", "000001.SZ"]
    assert orders["ts_code"].tolist() == ["002001.SZ", "000001.SZ"]
    assert [outcome.ts_code for outcome in outcomes] == ["002001.SZ", "000001.SZ"]


def test_selection_policy_deterministically_ranks_filters_and_limits(settings):
    from kfcquant.models import CandidateScore, FactorBreakdown

    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    policy = settings.selection.model_copy(
        update={"top_n": 2, "candidate_limit": 3, "minimum_opportunity_score": 50.0}
    )
    candidates = [
        CandidateScore(
            run_id="selection",
            ts_code=code,
            name=code,
            rank=99,
            opportunity_score=score,
            factor_breakdown=FactorBreakdown(),
            blocked=blocked,
            quote_at=at,
        )
        for code, score, blocked in (
            ("603001.SH", 80.0, False),
            ("600000.SH", 80.0, False),
            ("601001.SH", 70.0, True),
            ("605001.SH", 49.0, False),
        )
    ]

    ranked = policy.rank_candidates(candidates)
    selected = policy.select_candidates(ranked)

    assert [(item.ts_code, item.rank) for item in ranked] == [
        ("600000.SH", 1),
        ("603001.SH", 2),
        ("601001.SH", 3),
    ]
    assert [item.ts_code for item in selected] == ["600000.SH", "603001.SH"]

    assert policy.select_frame(pd.DataFrame()).empty
    with pytest.raises(ValueError, match="missing selection columns"):
        policy.select_frame(pd.DataFrame({"rank": [1]}))


def test_llm_hard_block_without_locatable_evidence_cannot_block_an_order(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}])
    )
    database.upsert_securities(make_securities([(code, code)]))
    database.upsert_daily_bars(make_daily([code], at))
    database.save_risk_events(
        [
            RiskEvent(
                event_id="unsupported-hard-block",
                document_id="fixture-document",
                ts_code=code,
                event_type="regulatory_investigation",
                direction="negative",
                severity="critical",
                confidence=1.0,
                hard_block=True,
                evidence="   ",
                published_at=at - timedelta(hours=1),
                extracted_at=at - timedelta(minutes=30),
                model_name="fixture",
            )
        ]
    )
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes([code], at)),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(at)

    candidate = database.get_candidates(run.run_id).iloc[0]
    assert not bool(candidate["blocked"])
    assert database.proposed_orders(run.run_id)["ts_code"].tolist() == [code]
