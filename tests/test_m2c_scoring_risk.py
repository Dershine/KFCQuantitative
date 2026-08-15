from __future__ import annotations

from datetime import datetime
from inspect import signature

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.models import SignalKind
from kfcquant.services.scoring import ScoringService
from kfcquant.strategy.risk import RiskPolicy
from kfcquant.strategy.scoring import ScoreModel


def _preclose_features() -> pd.DataFrame:
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    return pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "name": "甲",
                "quote_at": at,
                "ret_5d": 0.01,
                "ret_20d": 0.03,
                "intraday_strength": 0.01,
                "close_location": 0.7,
                "projected_volume_ratio": 1.2,
                "median_amount_20d": 200_000_000.0,
                "volatility_20d": 0.02,
                "gap_abs": 0.003,
                "limit_proximity": 0.0,
            },
            {
                "ts_code": "000001.SZ",
                "name": "乙",
                "quote_at": at,
                "ret_5d": 0.02,
                "ret_20d": 0.04,
                "intraday_strength": 0.02,
                "close_location": 0.8,
                "projected_volume_ratio": 1.4,
                "median_amount_20d": 250_000_000.0,
                "volatility_20d": 0.03,
                "gap_abs": 0.004,
                "limit_proximity": 0.1,
            },
        ]
    )


def test_score_model_is_deterministic_and_has_no_intelligence_inputs():
    parameters = set(signature(ScoreModel.score_preclose).parameters)
    assert {"risk_events", "news", "llm", "unprocessed_official_codes"}.isdisjoint(parameters)

    model = ScoreModel()
    first = model.score_preclose(_preclose_features())
    second = model.score_preclose(_preclose_features())

    pd.testing.assert_frame_equal(first, second)
    assert first["technical_score"].between(0.0, 90.0).all()
    assert model.score_preclose(pd.DataFrame()).empty
    assert model.score_morning(pd.DataFrame()).empty


def test_intelligence_adjustments_do_not_change_the_deterministic_technical_score(settings):
    features = _preclose_features()
    event = pd.DataFrame(
        [
            {
                "event_id": "positive",
                "ts_code": "600000.SH",
                "direction": "positive",
                "severity": "low",
                "confidence": 1.0,
                "hard_block": False,
                "event_type": "positive_update",
                "evidence": "中标公告",
            }
        ]
    )
    service = ScoringService(settings)

    baseline = service.score_preclose_features("baseline", features)
    adjusted = service.score_preclose_features("adjusted", features, event)
    baseline_candidate = {item.ts_code: item for item in baseline.candidates}["600000.SH"]
    adjusted_candidate = {item.ts_code: item for item in adjusted.candidates}["600000.SH"]

    assert adjusted_candidate.factor_breakdown.technical_score == baseline_candidate.factor_breakdown.technical_score
    assert adjusted_candidate.opportunity_score > baseline_candidate.opportunity_score


def test_risk_policy_separates_soft_adjustments_from_evidence_backed_hard_blocks():
    events = pd.DataFrame(
        [
            {
                "event_id": "positive",
                "ts_code": "600000.SH",
                "direction": "positive",
                "severity": "low",
                "confidence": 1.0,
                "hard_block": False,
                "event_type": "positive_update",
                "evidence": "中标公告",
            },
            {
                "event_id": "negative",
                "ts_code": "600000.SH",
                "direction": "negative",
                "severity": "high",
                "confidence": 1.0,
                "hard_block": False,
                "event_type": "other_risk",
                "evidence": "业绩下修",
            },
            {
                "event_id": "unsupported-block",
                "ts_code": "600000.SH",
                "direction": "negative",
                "severity": "critical",
                "confidence": 1.0,
                "hard_block": True,
                "event_type": "regulatory_investigation",
                "evidence": "   ",
            },
            {
                "event_id": "supported-block",
                "ts_code": "000001.SZ",
                "direction": "negative",
                "severity": "critical",
                "confidence": 1.0,
                "hard_block": True,
                "event_type": "regulatory_investigation",
                "evidence": "收到立案调查通知书",
            },
            {
                "event_id": "missing-evidence",
                "ts_code": "600000.SH",
                "direction": "neutral",
                "severity": "low",
                "confidence": 1.0,
                "hard_block": True,
                "event_type": "other_risk",
                "evidence": None,
            },
            {
                "event_id": "market-wide",
                "ts_code": None,
                "direction": "neutral",
                "severity": "low",
                "confidence": 1.0,
                "hard_block": False,
                "event_type": "neutral_update",
                "evidence": "市场信息",
            },
        ]
    )
    policy = RiskPolicy(events, unprocessed_official_codes={"002001.SZ"})

    soft = policy.assess("600000.SH", SignalKind.PRECLOSE_ENTRY)
    assert soft.news_score == 2.5
    assert soft.news_penalty == 20.0
    assert not soft.blocked
    assert soft.block_reasons == ()

    supported = policy.assess("000001.SZ", SignalKind.PRECLOSE_ENTRY)
    assert supported.blocked
    assert supported.block_reasons == ("regulatory_investigation: 收到立案调查通知书",)

    unprocessed = policy.assess("002001.SZ", SignalKind.MORNING_WATCHLIST)
    assert unprocessed.blocked
    assert unprocessed.block_reasons == ("存在尚未完成抽取的官方公告",)
