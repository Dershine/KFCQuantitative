from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import PaperOrder, PaperPosition, SignalKind
from kfcquant.services.portfolio import FeeModel
from kfcquant.services.workflow import Workflow
from kfcquant.strategy import (
    StrategyIdentity,
    StrategyParameterSnapshot,
    StrategyRegistry,
    build_default_strategy_registry,
)
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


class IdentifiedStrategy:
    def __init__(self, delegate, identity: StrategyIdentity):
        self.delegate = delegate
        self.signal_kind = delegate.signal_kind
        self.identity = identity
        self.requirements = delegate.requirements

    def evaluate(self, context):
        return self.delegate.evaluate(context)


def test_parameter_snapshot_is_canonical_stable_and_type_sensitive():
    first = StrategyParameterSnapshot.from_mapping(
        {"selection": {"top_n": 10, "threshold": 50.0}, "enabled": True}
    )
    reordered = StrategyParameterSnapshot.from_mapping(
        {"enabled": True, "selection": {"threshold": 50.0, "top_n": 10}}
    )
    changed_value = StrategyParameterSnapshot.from_mapping(
        {"enabled": True, "selection": {"threshold": 51.0, "top_n": 10}}
    )
    changed_type = StrategyParameterSnapshot.from_mapping(
        {"enabled": True, "selection": {"threshold": 50.0, "top_n": "10"}}
    )

    assert first.canonical_json == reordered.canonical_json
    assert first.parameter_hash == reordered.parameter_hash
    assert changed_value.parameter_hash != first.parameter_hash
    assert changed_type.parameter_hash != first.parameter_hash
    assert first.as_dict() == {
        "enabled": True,
        "selection": {"threshold": 50.0, "top_n": 10},
    }


@pytest.mark.parametrize(
    "parameters",
    [
        {"invalid": float("nan")},
        {"invalid": float("inf")},
        {1: "non-string-key"},
        {"invalid": {"sets-are-not-json": {1, 2}}},
    ],
)
def test_parameter_snapshot_rejects_ambiguous_or_non_finite_values(parameters):
    with pytest.raises((TypeError, ValueError)):
        StrategyParameterSnapshot.from_mapping(parameters)


def test_parameter_snapshot_normalizes_supported_json_boundary_types():
    class FixtureMode(Enum):
        ENABLED = 1

    snapshot = StrategyParameterSnapshot.from_mapping(
        {
            "kind": SignalKind.PRECLOSE_ENTRY,
            "mode": FixtureMode.ENABLED,
            "date": date(2026, 8, 10),
            "time": time(14, 40),
            "sequence": (None, True, 1, "value"),
        }
    )

    assert snapshot.as_dict() == {
        "date": "2026-08-10",
        "kind": "preclose_entry",
        "mode": 1,
        "sequence": [None, True, 1, "value"],
        "time": "14:40:00",
    }
    assert StrategyParameterSnapshot.empty().as_dict() == {}


@pytest.mark.parametrize(
    ("canonical_json", "hash_value", "message"),
    [
        ("[]", "0" * 64, "JSON object"),
        ('{"b":1,"a":2}', "0" * 64, "canonical JSON"),
        ('{"a":1}', "0" * 64, "hash does not match"),
    ],
)
def test_parameter_snapshot_constructor_rejects_invalid_serialized_identity(
    canonical_json, hash_value, message
):
    with pytest.raises(ValueError, match=message):
        StrategyParameterSnapshot(canonical_json, hash_value)


def test_default_strategy_parameters_are_explicit_and_exclude_secrets(settings):
    configured = settings.model_copy(
        update={
            "llm_api_key": "must-not-be-persisted",
            "tushare_token": "must-not-be-persisted-either",
            "selection": settings.selection.model_copy(update={"top_n": 7}),
        }
    )
    registry = build_default_strategy_registry(configured)

    morning = registry.resolve(SignalKind.MORNING_WATCHLIST).identity.parameter_snapshot.as_dict()
    preclose = registry.resolve(SignalKind.PRECLOSE_ENTRY).identity.parameter_snapshot.as_dict()
    serialized = f"{morning!r}{preclose!r}"

    assert morning["selection"]["top_n"] == 7
    assert preclose["selection"]["top_n"] == 7
    assert preclose["features"]["quote_freshness_seconds"] == configured.quote_freshness_seconds
    assert "must-not-be-persisted" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized


def test_strategy_identity_reaches_run_orders_position_and_outcomes(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    next_day = at + timedelta(days=1)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [
                {"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()},
                {"cal_date": next_day.date(), "is_open": True, "pretrade_date": at.date()},
            ]
        )
    )
    database.upsert_securities(make_securities([(code, code)]))
    database.upsert_daily_bars(make_daily([code], at))

    defaults = build_default_strategy_registry(settings)
    morning = defaults.resolve(SignalKind.MORNING_WATCHLIST)
    snapshot = StrategyParameterSnapshot.from_mapping(
        {"selection": {"top_n": 1}, "fixture_threshold": 12.5}
    )
    identity = StrategyIdentity("m2d-preclose", "m2d-v1", snapshot)
    preclose = IdentifiedStrategy(defaults.resolve(SignalKind.PRECLOSE_ENTRY), identity)
    live = FakeLive(make_quotes([code], at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=live,
        llm_provider=FakeLLM(),
        strategy_registry=StrategyRegistry([morning, preclose]),
    )

    run = workflow.run_preclose(at)
    stored_run = database.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
    order_row = database.proposed_orders(run.run_id).iloc[0].to_dict()
    order = PaperOrder.model_validate(order_row)
    fill, position = FeeModel(settings).buy_fill(10.0, 1000, at + timedelta(minutes=5), order)
    database.apply_buy_fill(fill, position)
    stored_position = PaperPosition.model_validate(database.get_open_positions().iloc[0].to_dict())

    live.bars = []
    candidate_outcomes = workflow.evaluation.evaluate(stored_run, next_day)
    workflow.portfolio._close_position(stored_position, 10.5, next_day, "take_profit")

    expected = {
        "strategy_id": identity.strategy_id,
        "strategy_version": identity.version,
        "parameter_hash": snapshot.parameter_hash,
        "strategy_parameters": snapshot.as_dict(),
    }
    assert {key: stored_run[key] for key in expected} == expected
    assert order.model_dump(include=set(expected)) == expected
    assert stored_position.model_dump(include=set(expected)) == expected
    assert candidate_outcomes[0].model_dump(include=set(expected)) == expected

    sell_order = PaperOrder.model_validate(
        database.table_with_strategy("paper_orders").query("side == 'sell'").iloc[0].to_dict()
    )
    opportunity = database.table_with_strategy("opportunity_outcomes").iloc[0].to_dict()
    assert sell_order.model_dump(include=set(expected)) == expected
    assert {key: opportunity[key] for key in expected} == expected

    attribution = database.table("strategy_attributions")
    assert set(attribution["entity_kind"]) == {
        "signal_run",
        "paper_order",
        "paper_position",
        "candidate_outcome",
        "opportunity_outcome",
    }
