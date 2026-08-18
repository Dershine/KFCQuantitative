from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.market_data import (
    DAILY_BAR_SCHEMA,
    LIVE_QUOTE_SCHEMA,
    SECURITY_SCHEMA,
    TRADE_CALENDAR_SCHEMA,
    LogicalType,
    MarketColumn,
    MarketDataValidationError,
    MarketRowRule,
    MarketTableSchema,
)


def _security_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "symbol": "600000",
                "name": "Fixture",
                "exchange": "SH",
                "market": "主板",
                "list_date": date(1999, 11, 10),
                "delist_date": None,
                "list_status": "L",
            }
        ]
    )


def _calendar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "cal_date": date(2026, 8, 10),
                "is_open": True,
                "pretrade_date": date(2026, 8, 7),
            }
        ]
    )


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "trade_date": date(2026, 8, 10),
                "open": 9.20,
                "high": 9.38,
                "low": 9.16,
                "close": 9.29,
                "pre_close": 9.21,
                "volume": 62_542_539.0,
                "amount": 581_544_471.0,
                "adj_factor": 1.0,
                "up_limit": 10.13,
                "down_limit": 8.29,
                "suspended": False,
                "is_st": False,
            }
        ]
    )


def _quote_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "captured_at": datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ),
                "price": 9.29,
                "open": 9.20,
                "high": 9.38,
                "low": 9.16,
                "pre_close": 9.21,
                "volume": 62_542_539.0,
                "amount": 581_544_471.0,
                "source": "fixture",
            }
        ]
    )


def test_market_schemas_are_versioned_and_preserve_explicit_units_and_keys():
    cases = [
        (SECURITY_SCHEMA, _security_frame(), ("ts_code",)),
        (TRADE_CALENDAR_SCHEMA, _calendar_frame(), ("cal_date",)),
        (DAILY_BAR_SCHEMA, _daily_frame(), ("ts_code", "trade_date")),
        (LIVE_QUOTE_SCHEMA, _quote_frame(), ("ts_code", "captured_at")),
    ]

    for schema, frame, unique_key in cases:
        validated = schema.validate(frame[list(reversed(frame.columns))])
        assert schema.version.endswith("-v1")
        assert schema.unique_key == unique_key
        assert tuple(validated.frame.columns) == schema.columns
        assert validated.row_count == 1

    assert DAILY_BAR_SCHEMA.units == {
        "open": "CNY/share",
        "high": "CNY/share",
        "low": "CNY/share",
        "close": "CNY/share",
        "pre_close": "CNY/share",
        "volume": "share",
        "amount": "CNY",
        "up_limit": "CNY/share",
        "down_limit": "CNY/share",
        "adj_factor": "ratio",
    }
    assert LIVE_QUOTE_SCHEMA.units["volume"] == "share"
    assert LIVE_QUOTE_SCHEMA.units["amount"] == "CNY"


@pytest.mark.parametrize(
    ("schema", "frame", "message"),
    [
        (SECURITY_SCHEMA, _security_frame().drop(columns=["list_date"]), "missing columns"),
        (SECURITY_SCHEMA, _security_frame().assign(provider_hint="raw"), "unexpected columns"),
        (SECURITY_SCHEMA, _security_frame().assign(name=[None]), "not nullable"),
        (
            SECURITY_SCHEMA,
            pd.concat([_security_frame(), _security_frame()], ignore_index=True),
            "duplicate unique key",
        ),
        (
            SECURITY_SCHEMA,
            _security_frame().assign(delist_date=[date(1999, 11, 9)]),
            "delist_on_or_after_list",
        ),
        (SECURITY_SCHEMA, _security_frame().assign(ts_code=["BAD"]), "does not match"),
        (SECURITY_SCHEMA, _security_frame().assign(exchange=["HK"]), "outside"),
        (
            SECURITY_SCHEMA,
            _security_frame().assign(list_date=[datetime(1999, 11, 10, tzinfo=SHANGHAI_TZ)]),
            "without a time component",
        ),
        (
            TRADE_CALENDAR_SCHEMA,
            _calendar_frame().assign(pretrade_date=[date(2026, 8, 10)]),
            "previous_trade_before_date",
        ),
        (DAILY_BAR_SCHEMA, _daily_frame().assign(high=[9.0]), "ohlc_bounds"),
        (DAILY_BAR_SCHEMA, _daily_frame().assign(amount=[float("inf")]), "finite"),
        (LIVE_QUOTE_SCHEMA, _quote_frame().assign(high=[9.0]), "ohlc_bounds_or_inactive"),
        (
            LIVE_QUOTE_SCHEMA,
            _quote_frame().assign(
                captured_at=[
                    datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ).replace(tzinfo=None)
                ]
            ),
            "timezone-aware",
        ),
        (LIVE_QUOTE_SCHEMA, _quote_frame().assign(volume=[-1.0]), "greater than or equal to 0"),
    ],
)
def test_market_schemas_reject_structural_type_key_and_relation_drift(schema, frame, message):
    with pytest.raises(MarketDataValidationError, match=message):
        schema.validate(frame)


def test_empty_provider_result_is_normalized_to_a_canonical_typed_boundary():
    for schema in (SECURITY_SCHEMA, TRADE_CALENDAR_SCHEMA, DAILY_BAR_SCHEMA, LIVE_QUOTE_SCHEMA):
        validated = schema.validate(pd.DataFrame())

        assert validated.frame.empty
        assert tuple(validated.frame.columns) == schema.columns
        assert validated.row_count == 0

    with pytest.raises(MarketDataValidationError, match="missing columns"):
        SECURITY_SCHEMA.validate(pd.DataFrame(columns=["ts_code"]))


def test_live_quote_schema_accepts_an_explicit_inactive_zero_state_for_domain_filtering():
    inactive = _quote_frame().assign(price=[0.0], open=[0.0], high=[0.0], low=[0.0], volume=[0.0], amount=[0.0])

    validated = LIVE_QUOTE_SCHEMA.validate(inactive)

    assert validated.row_count == 1


def test_schema_rejects_non_frame_and_malformed_custom_row_rule():
    with pytest.raises(MarketDataValidationError, match="pandas DataFrame"):
        SECURITY_SCHEMA.validate([])  # type: ignore[arg-type]

    malformed = MarketTableSchema(
        name="malformed",
        version="malformed-v1",
        fields=(MarketColumn("flag", LogicalType.BOOLEAN),),
        unique_key=("flag",),
        row_rules=(MarketRowRule("not_aligned", lambda _frame: pd.Series(dtype=bool)),),
    )
    with pytest.raises(TypeError, match="row-aligned Series"):
        malformed.validate(pd.DataFrame({"flag": [True]}))


def test_schema_rejects_duplicate_column_names_before_field_validation():
    duplicate = pd.concat([_security_frame(), _security_frame()[["name"]]], axis=1)

    with pytest.raises(MarketDataValidationError, match="duplicate columns: name"):
        SECURITY_SCHEMA.validate(duplicate)
