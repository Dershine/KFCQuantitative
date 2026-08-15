from __future__ import annotations

from datetime import datetime

import numpy as np

from kfcquant.config import SHANGHAI_TZ
from kfcquant.strategy.universe import UniversePolicy, is_shenzhen_shanghai_main_board
from tests.conftest import make_daily, make_securities


def test_universe_policy_applies_each_stock_pool_rule_independently(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = [
        "600000.SH",  # eligible
        "300001.SZ",  # other board
        "601001.SH",  # risky name
        "603001.SH",  # inactive listing
        "605001.SH",  # insufficient listing history
        "000001.SZ",  # suspended
        "001001.SZ",  # dated ST state
        "002001.SZ",  # insufficient liquidity
    ]
    securities = make_securities([(code, "ST风险" if code == "601001.SH" else code) for code in codes])
    securities.loc[securities["ts_code"] == "603001.SH", "list_status"] = "D"
    bars = make_daily(codes, at)
    bars["is_st"] = False
    bars = bars[~((bars["ts_code"] == "605001.SH") & (bars.groupby("ts_code").cumcount() < 100))]
    latest = bars["trade_date"] == bars["trade_date"].max()
    bars.loc[latest & (bars["ts_code"] == "000001.SZ"), "suspended"] = True
    bars.loc[latest & (bars["ts_code"] == "001001.SZ"), "is_st"] = True
    bars.loc[bars["ts_code"] == "002001.SZ", "amount"] = 1_000_000

    selection = UniversePolicy.from_settings(settings).select(securities, bars)

    assert selection.eligible_codes == ("600000.SH",)
    assert selection.securities["ts_code"].tolist() == ["600000.SH"]
    assert set(selection.bars["ts_code"]) == {"600000.SH"}
    assert selection.exclusion_counts == {
        "non_main_board": 1,
        "risk_name": 1,
        "inactive_listing": 1,
        "insufficient_listing_history": 1,
        "suspended": 1,
        "historical_st": 1,
        "insufficient_liquidity": 1,
        "eligible": 1,
    }


def test_universe_policy_reports_missing_core_data(settings):
    selection = UniversePolicy.from_settings(settings).select(
        make_securities([]), make_daily([], datetime.now(SHANGHAI_TZ))
    )

    assert selection.eligible_codes == ()
    assert selection.exclusion_counts == {"missing_core_data": 1, "eligible": 0}
    assert not is_shenzhen_shanghai_main_board("malformed")


def test_universe_policy_reports_missing_history_and_unusable_liquidity_history(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "601001.SH", "603001.SH"]
    securities = make_securities([(code, code) for code in codes])
    bars = make_daily(codes[:2], at)
    latest_dates = sorted(bars.loc[bars["ts_code"] == codes[1], "trade_date"].unique())[-20:]
    bars.loc[(bars["ts_code"] == codes[1]) & bars["trade_date"].isin(latest_dates), "amount"] = np.nan

    selection = UniversePolicy.from_settings(settings).select(securities, bars)

    assert selection.eligible_codes == (codes[0],)
    assert selection.exclusion_counts == {
        "insufficient_liquidity_history": 1,
        "missing_history": 1,
        "eligible": 1,
    }
