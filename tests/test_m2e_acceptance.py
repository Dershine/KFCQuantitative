from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.models import SignalKind
from kfcquant.policies import SchedulePolicy, SelectionPolicy
from kfcquant.strategy import StrategyContext, build_default_strategy_registry
from tests.conftest import make_daily, make_quotes, make_securities

GOLDEN_SNAPSHOT = Path(__file__).parent / "fixtures" / "m2e_strategy_golden.json"


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _normalize(value.item())
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 12)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize(item) for item in value]
    raise TypeError(f"golden fixture cannot normalize {type(value).__name__}")


def _frame_records(frame: pd.DataFrame, sort_by: list[str]) -> list[dict[str, Any]]:
    ordered = frame.sort_values(sort_by, kind="stable").reset_index(drop=True)
    return [_normalize(record) for record in ordered.to_dict("records")]


def _fixture_inputs() -> dict[str, Any]:
    morning_at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    preclose_at = morning_at.replace(hour=14, minute=40)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    securities = make_securities([(code, f"Company {index}") for index, code in enumerate(codes, start=1)])
    bars = make_daily(codes, morning_at)
    quotes = make_quotes(codes, preclose_at)
    risk_events = pd.DataFrame(
        [
            {
                "event_id": "positive-evidence",
                "ts_code": "000001.SZ",
                "direction": "positive",
                "severity": "low",
                "confidence": 0.8,
                "hard_block": False,
                "event_type": "order_growth",
                "evidence": "order growth",
            },
            {
                "event_id": "supported-hard-block",
                "ts_code": "600000.SH",
                "direction": "negative",
                "severity": "critical",
                "confidence": 1.0,
                "hard_block": True,
                "event_type": "regulatory_investigation",
                "evidence": "formal investigation",
            },
            {
                "event_id": "unsupported-hard-block",
                "ts_code": "002001.SZ",
                "direction": "negative",
                "severity": "high",
                "confidence": 1.0,
                "hard_block": True,
                "event_type": "unverified_rumor",
                "evidence": "   ",
            },
        ]
    )
    return {
        "morning_at": morning_at,
        "preclose_at": preclose_at,
        "securities": securities,
        "bars": bars,
        "quotes": quotes,
        "risk_events": risk_events,
        "previous_signal_codes": frozenset({"000001.SZ", "002001.SZ"}),
    }


def _input_payload(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "morning_at": _normalize(inputs["morning_at"]),
        "preclose_at": _normalize(inputs["preclose_at"]),
        "securities": _frame_records(inputs["securities"], ["ts_code"]),
        "bars": _frame_records(inputs["bars"], ["ts_code", "trade_date"]),
        "quotes": _frame_records(inputs["quotes"], ["ts_code", "captured_at"]),
        "risk_events": _frame_records(inputs["risk_events"], ["event_id"]),
        "previous_signal_codes": sorted(inputs["previous_signal_codes"]),
    }


def _result_snapshot(strategy: Any, result: Any) -> dict[str, Any]:
    return {
        "identity": _normalize(strategy.identity.attribution_fields()),
        "eligible_count": result.eligible_count,
        "exclusion_counts": _normalize(result.exclusion_counts),
        "candidates": [
            _normalize(candidate.model_dump(mode="json", exclude={"run_id"}))
            for candidate in result.candidates
        ],
    }


def _evaluate_snapshot(settings: Settings) -> dict[str, Any]:
    inputs = _fixture_inputs()
    input_json = json.dumps(
        _input_payload(inputs), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    registry = build_default_strategy_registry(settings)
    morning = registry.resolve(SignalKind.MORNING_WATCHLIST)
    preclose = registry.resolve(SignalKind.PRECLOSE_ENTRY)
    morning_result = morning.evaluate(
        StrategyContext(
            run_id="m2e-morning",
            signal_kind=SignalKind.MORNING_WATCHLIST,
            as_of=inputs["morning_at"],
            information_cutoff=inputs["morning_at"],
            securities=inputs["securities"],
            bars=inputs["bars"],
            risk_events=inputs["risk_events"],
        )
    )
    preclose_result = preclose.evaluate(
        StrategyContext(
            run_id="m2e-preclose",
            signal_kind=SignalKind.PRECLOSE_ENTRY,
            as_of=inputs["preclose_at"],
            information_cutoff=inputs["preclose_at"],
            securities=inputs["securities"],
            bars=inputs["bars"],
            quotes=inputs["quotes"],
            risk_events=inputs["risk_events"],
            previous_signal_codes=inputs["previous_signal_codes"],
        )
    )
    return {
        "fixture_schema_version": 2,
        "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
        "strategies": {
            SignalKind.MORNING_WATCHLIST.value: _result_snapshot(morning, morning_result),
            SignalKind.PRECLOSE_ENTRY.value: _result_snapshot(preclose, preclose_result),
        },
    }


def test_m2e_fixed_input_identity_parameters_and_candidates_match_reviewed_golden(settings):
    configured = settings.model_copy(
        update={
            "min_listing_trading_days": 120,
            "min_median_amount_20d": 100_000_000.0,
            "quote_freshness_seconds": 60,
            "limit_distance_fraction": 0.01,
            "strategy_version_morning": "morning-v1",
            "strategy_version_preclose": "preclose-v2",
            "schedule": SchedulePolicy(),
            "selection": SelectionPolicy(),
            "news_lookback_trading_days": 5,
        }
    )
    actual = _evaluate_snapshot(configured)

    assert actual == _evaluate_snapshot(configured), "the same fixed strategy snapshot must be deterministic"
    assert GOLDEN_SNAPSHOT.exists(), "M2-E requires a reviewed, version-controlled Golden Snapshot"
    expected = json.loads(GOLDEN_SNAPSHOT.read_text(encoding="utf-8"))
    assert actual == expected, "strategy output drifted; review the change before updating the Golden Snapshot"
