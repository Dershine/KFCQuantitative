from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import RiskEvent, SignalKind
from kfcquant.services.workflow import Workflow
from kfcquant.strategy import (
    StrategyContext,
    StrategyIdentity,
    StrategyRegistry,
    build_default_strategy_registry,
)
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


def test_morning_and_preclose_use_registry_contract_without_changing_safety_gates(settings):
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

    defaults = build_default_strategy_registry(settings)
    morning = RecordingStrategy(
        defaults.resolve(SignalKind.MORNING_WATCHLIST), StrategyIdentity("fixture-morning", "registry-morning-v9")
    )
    preclose = RecordingStrategy(
        defaults.resolve(SignalKind.PRECLOSE_ENTRY), StrategyIdentity("fixture-preclose", "registry-preclose-v9")
    )
    registry = StrategyRegistry([morning, preclose])
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, preclose_at)),
        llm_provider=FakeLLM(),
        strategy_registry=registry,
    )

    morning_run = workflow.run_morning(morning_at)
    preclose_run = workflow.run_preclose(preclose_at)

    assert [context.signal_kind for context in morning.contexts] == [SignalKind.MORNING_WATCHLIST]
    assert [context.signal_kind for context in preclose.contexts] == [SignalKind.PRECLOSE_ENTRY]
    assert not morning.requirements.requires_quotes
    assert preclose.requirements.requires_quotes
    assert preclose.requirements.requires_previous_signals
    assert morning.contexts[0].information_cutoff == morning_run.information_cutoff
    assert preclose.contexts[0].previous_signal_codes == frozenset(codes)
    assert morning_run.strategy_version == morning.identity.version
    assert preclose_run.strategy_version == preclose.identity.version
    assert not morning_run.tradable
    assert database.proposed_orders(morning_run.run_id).empty
    assert preclose_run.tradable
    assert len(database.proposed_orders(preclose_run.run_id)) == len(codes)


def test_workflow_fails_closed_when_registry_is_incomplete(settings):
    defaults = build_default_strategy_registry(settings)
    incomplete = StrategyRegistry([defaults.resolve(SignalKind.MORNING_WATCHLIST)])

    try:
        Workflow(settings, strategy_registry=incomplete)
    except ValueError as exc:
        assert "preclose_entry" in str(exc)
    else:
        raise AssertionError("an incomplete strategy registry must be rejected")


def test_blocked_candidate_from_registered_strategy_still_cannot_create_order(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}])
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    database.save_risk_events(
        [
            RiskEvent(
                event_id="hard-block",
                document_id="fixture-document",
                ts_code=codes[0],
                event_type="regulatory_investigation",
                direction="negative",
                severity="critical",
                confidence=1.0,
                hard_block=True,
                evidence="立案调查",
                source_url=None,
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
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(at)

    candidates = database.get_candidates(run.run_id).set_index("ts_code")
    orders = database.proposed_orders(run.run_id)
    assert bool(candidates.loc[codes[0], "blocked"])
    assert codes[0] not in set(orders["ts_code"])
    assert codes[1] in set(orders["ts_code"])
