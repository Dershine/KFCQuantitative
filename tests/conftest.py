from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from kfcquant.config import Settings
from kfcquant.strategy import StrategyIdentity, StrategyParameterSnapshot


def strategy_attribution(
    strategy_id: str = "fixture-strategy",
    version: str = "fixture-v1",
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    snapshot = StrategyParameterSnapshot.from_mapping(parameters or {"fixture": True})
    return StrategyIdentity(strategy_id, version, snapshot).attribution_fields()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "test.duckdb",
        raw_data_dir=tmp_path / "raw",
        report_dir=tmp_path / "reports",
        runtime_dir=tmp_path / "runtime",
        backup_dir=tmp_path / "backups",
        initial_cash=100_000,
        min_listing_trading_days=120,
        min_median_amount_20d=100_000_000,
    )


def make_securities(codes: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": code,
                "symbol": code.split(".")[0],
                "name": name,
                "exchange": code.split(".")[1],
                "market": "主板",
                "list_date": date(2010, 1, 1),
                "delist_date": None,
                "list_status": "L",
            }
            for code, name in codes
        ]
    )


def make_daily(codes: list[str], end: datetime, days: int = 130) -> pd.DataFrame:
    dates = pd.bdate_range(end=(end - timedelta(days=1)).date(), periods=days)
    rows = []
    for index, code in enumerate(codes, start=1):
        for offset, trade_date in enumerate(dates):
            close = 10 + index + offset * (0.01 * index)
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": trade_date.date(),
                    "open": close - 0.03,
                    "high": close + 0.08,
                    "low": close - 0.08,
                    "close": close,
                    "pre_close": close - 0.01,
                    "volume": 20_000_000,
                    "amount": 200_000_000 + index * 10_000_000,
                    "adj_factor": 1.0,
                    "up_limit": round((close - 0.01) * 1.1, 2),
                    "down_limit": round((close - 0.01) * 0.9, 2),
                    "suspended": False,
                    "is_st": False,
                }
            )
    return pd.DataFrame(rows)


def make_quotes(codes: list[str], at: datetime) -> pd.DataFrame:
    rows = []
    for index, code in enumerate(codes, start=1):
        pre_close = 11 + index
        open_price = pre_close * 1.003
        price = pre_close * (1.006 + index * 0.001)
        rows.append(
            {
                "ts_code": code,
                "captured_at": at,
                "price": price,
                "open": open_price,
                "high": price * 1.002,
                "low": open_price * 0.998,
                "pre_close": pre_close,
                "volume": 10_000_000 + index * 100_000,
                "amount": 140_000_000 + index * 10_000_000,
                "source": "fixture",
            }
        )
    return pd.DataFrame(rows)
