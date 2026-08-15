from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.models import SignalKind
from kfcquant.strategy import (
    StrategyContext,
    StrategyIdentity,
    StrategyRegistry,
    StrategyRequirements,
    StrategyResult,
    build_default_strategy_registry,
)


class FixedStrategy:
    def __init__(self, signal_kind: SignalKind, strategy_id: str = "fixture", version: str = "v1"):
        self.signal_kind = signal_kind
        self.identity = StrategyIdentity(strategy_id=strategy_id, version=version)
        self.requirements = StrategyRequirements()

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        assert context.signal_kind == self.signal_kind
        return StrategyResult(candidates=[], eligible_count=0, exclusion_counts={})


def test_strategy_contract_is_immutable_and_time_bounded():
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    context = StrategyContext(
        run_id="run-1",
        signal_kind=SignalKind.PRECLOSE_ENTRY,
        as_of=at,
        information_cutoff=at,
        securities=pd.DataFrame(),
        bars=pd.DataFrame(),
        quotes=pd.DataFrame(),
    )

    assert context.information_cutoff == at
    with pytest.raises(FrozenInstanceError):
        context.run_id = "changed"
    with pytest.raises(ValueError, match="timezone"):
        StrategyContext(
            run_id="run-2",
            signal_kind=SignalKind.MORNING_WATCHLIST,
            as_of=at.replace(tzinfo=None),
            information_cutoff=at,
            securities=pd.DataFrame(),
            bars=pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="after as_of"):
        StrategyContext(
            run_id="run-3",
            signal_kind=SignalKind.MORNING_WATCHLIST,
            as_of=at,
            information_cutoff=at.replace(hour=15),
            securities=pd.DataFrame(),
            bars=pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="information_cutoff.*timezone"):
        StrategyContext(
            run_id="run-4",
            signal_kind=SignalKind.MORNING_WATCHLIST,
            as_of=at,
            information_cutoff=at.replace(tzinfo=None),
            securities=pd.DataFrame(),
            bars=pd.DataFrame(),
        )


@pytest.mark.parametrize("value", ["", " contains spaces", "bad/version"])
def test_strategy_identity_rejects_free_form_identifiers(value):
    with pytest.raises(ValueError):
        StrategyIdentity(strategy_id=value, version="v1")
    with pytest.raises(ValueError):
        StrategyIdentity(strategy_id="fixture", version=value)


def test_registry_rejects_duplicate_and_missing_signal_kinds():
    morning = FixedStrategy(SignalKind.MORNING_WATCHLIST, "morning")
    duplicate = FixedStrategy(SignalKind.MORNING_WATCHLIST, "morning-2")
    registry = StrategyRegistry([morning])

    assert registry.resolve(SignalKind.MORNING_WATCHLIST) is morning
    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate)
    with pytest.raises(LookupError, match="preclose_entry"):
        registry.resolve(SignalKind.PRECLOSE_ENTRY)


def test_default_strategies_reject_context_for_the_other_signal_kind(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    registry = build_default_strategy_registry(settings)
    morning_context = StrategyContext(
        run_id="morning",
        signal_kind=SignalKind.MORNING_WATCHLIST,
        as_of=at,
        information_cutoff=at,
        securities=pd.DataFrame(),
        bars=pd.DataFrame(),
    )
    preclose_context = StrategyContext(
        run_id="preclose",
        signal_kind=SignalKind.PRECLOSE_ENTRY,
        as_of=at,
        information_cutoff=at,
        securities=pd.DataFrame(),
        bars=pd.DataFrame(),
    )

    with pytest.raises(ValueError, match="morning strategy"):
        registry.resolve(SignalKind.MORNING_WATCHLIST).evaluate(preclose_context)
    with pytest.raises(ValueError, match="pre-close strategy"):
        registry.resolve(SignalKind.PRECLOSE_ENTRY).evaluate(morning_context)
