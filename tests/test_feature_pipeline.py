from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.strategy.features import (
    MORNING_FEATURE_SCHEMA,
    PRECLOSE_FEATURE_SCHEMA,
    FeaturePipeline,
    FeatureSchema,
    trading_minutes_elapsed,
)
from kfcquant.strategy.universe import UniversePolicy
from tests.conftest import make_daily, make_quotes, make_securities


def _universe(settings, at, codes):
    return UniversePolicy.from_settings(settings).select(
        make_securities([(code, code) for code in codes]),
        make_daily(codes, at),
    )


def test_feature_pipeline_has_explicit_versioned_schemas_and_no_risk_or_ranking_inputs(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    pipeline = FeaturePipeline.from_settings(settings)

    morning = pipeline.build_morning(_universe(settings, at, codes), at)
    preclose = pipeline.build_preclose(_universe(settings, at, codes), make_quotes(codes, at), at)

    assert morning.schema is MORNING_FEATURE_SCHEMA
    assert preclose.schema is PRECLOSE_FEATURE_SCHEMA
    assert morning.schema.version == "morning-features-v1"
    assert preclose.schema.version == "preclose-features-v1"
    assert tuple(morning.frame.columns) == morning.schema.columns
    assert tuple(preclose.frame.columns) == preclose.schema.columns
    forbidden = {"news_score", "risk_penalty", "blocked", "rank", "opportunity_score"}
    assert forbidden.isdisjoint(morning.frame.columns)
    assert forbidden.isdisjoint(preclose.frame.columns)
    signature = inspect.signature(pipeline.build_preclose)
    assert {"risk_events", "news", "selection", "candidate_limit"}.isdisjoint(signature.parameters)


def test_preclose_feature_pipeline_is_deterministic_and_rejects_stale_quotes(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    universe = _universe(settings, at, codes)
    quotes = make_quotes(codes, at)
    quotes.loc[quotes["ts_code"] == codes[1], "captured_at"] = at - timedelta(minutes=2)
    pipeline = FeaturePipeline.from_settings(settings)

    first = pipeline.build_preclose(universe, quotes, at)
    second = pipeline.build_preclose(universe, quotes, at)

    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert first.frame["ts_code"].tolist() == [codes[0]]
    assert first.exclusion_counts == {"stale_quote": 1, "eligible_features": 1}


def test_feature_pipeline_reports_missing_quotes_and_insufficient_history(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    universe = _universe(settings, at, codes)
    pipeline = FeaturePipeline.from_settings(settings)

    empty_universe = UniversePolicy.from_settings(settings).select(pd.DataFrame(), pd.DataFrame())
    assert pipeline.build_morning(empty_universe, at).exclusion_counts == {"eligible_features": 0}

    missing_quotes = pipeline.build_preclose(universe, make_quotes(codes[:1], at), at)
    assert missing_quotes.frame["ts_code"].tolist() == codes[:1]
    assert missing_quotes.exclusion_counts == {"missing_quote": 1, "eligible_features": 1}
    no_quotes = pipeline.build_preclose(universe, pd.DataFrame(), at)
    assert no_quotes.exclusion_counts == {"missing_quote": 2, "eligible_features": 0}

    sparse_bars = universe.bars.copy()
    sparse_bars.loc[sparse_bars.groupby("ts_code").cumcount() >= 109, "close"] = None
    sparse = replace(universe, bars=sparse_bars)
    morning = pipeline.build_morning(sparse, at)
    preclose = pipeline.build_preclose(sparse, make_quotes(codes, at), at)
    assert morning.exclusion_counts == {"insufficient_feature_history": 2, "eligible_features": 0}
    assert preclose.exclusion_counts == {"insufficient_feature_history": 2, "eligible_features": 0}


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"open": 0.0}, "invalid_quote"),
        ({"price": 13.1}, "near_up_limit"),
        ({"price": 10.9}, "near_down_limit"),
        ({"captured_at": None}, "stale_quote"),
    ],
)
def test_preclose_feature_pipeline_fails_closed_for_invalid_market_inputs(settings, updates, reason):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    quotes = make_quotes([code], at)
    for column, value in updates.items():
        quotes.loc[0, column] = value

    result = FeaturePipeline.from_settings(settings).build_preclose(_universe(settings, at, [code]), quotes, at)

    assert result.frame.empty
    assert result.exclusion_counts == {reason: 1, "eligible_features": 0}


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(9, 29, 0), (10, 0, 30), (12, 0, 120), (14, 0, 180), (16, 0, 240)],
)
def test_trading_minutes_elapsed_covers_the_full_session(hour, minute, expected):
    at = datetime(2026, 8, 10, hour, minute, tzinfo=SHANGHAI_TZ)

    assert trading_minutes_elapsed(at) == expected


def test_feature_schema_rejects_implicit_or_wrongly_typed_columns():
    schema = FeatureSchema(
        name="fixture",
        version="fixture-v1",
        fields=PRECLOSE_FEATURE_SCHEMA.fields,
    )
    frame = pd.DataFrame([{column: 1.0 for column in schema.columns}])

    with pytest.raises(ValueError, match="missing columns"):
        schema.validate(pd.DataFrame(columns=schema.columns[1:]))
    with pytest.raises(ValueError, match="ts_code"):
        schema.validate(frame)
    with pytest.raises(ValueError, match="unexpected"):
        schema.validate(pd.DataFrame(columns=[*schema.columns, "ranking_hint"]))

    valid_row = {
        field.name.value: (
            "fixture"
            if field.dtype == "string"
            else pd.Timestamp("2026-08-10T14:40:00+08:00")
            if field.dtype == "datetime"
            else 1.0
        )
        for field in schema.fields
    }
    numeric_error = pd.DataFrame([{**valid_row, "ret_5d": "bad"}])
    datetime_error = pd.DataFrame([{**valid_row, "quote_at": "bad"}])
    with pytest.raises(ValueError, match="ret_5d"):
        schema.validate(numeric_error)
    with pytest.raises(ValueError, match="quote_at"):
        schema.validate(datetime_error)
