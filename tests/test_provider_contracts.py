from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.market_data import (
    DAILY_BAR_SCHEMA,
    LIVE_QUOTE_SCHEMA,
    SECURITY_SCHEMA,
    TRADE_CALENDAR_SCHEMA,
    MarketDataValidationError,
)
from kfcquant.providers.akshare_live import AkShareLiveQuoteProvider
from kfcquant.providers.baostock_market import BaoStockMarketDataProvider
from kfcquant.providers.tushare import TushareProvider
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_free_providers import FakeAkShareQuotes, FakeBaoStock, Result
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket, FakeRangeMarket


class FakeTusharePro:
    def __init__(self, fail_method: str | None = None):
        self.fail_method = fail_method
        self.suspend_call: dict[str, str] | None = None
        self.stock_st_call: dict[str, str] | None = None

    def stock_basic(self, *, list_status: str, **_kwargs) -> pd.DataFrame:
        if list_status != "L":
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "symbol": "600000",
                    "name": "浦发银行",
                    "area": "上海",
                    "industry": "银行",
                    "market": "主板",
                    "exchange": "SSE",
                    "list_status": "L",
                    "list_date": "19991110",
                    "delist_date": None,
                }
            ]
        )

    def trade_cal(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"cal_date": "20260809", "is_open": "0", "pretrade_date": "20260807"},
                {"cal_date": "20260810", "is_open": "1", "pretrade_date": "20260807"},
            ]
        )

    def daily(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20260810",
                    "open": "9.20",
                    "high": "9.38",
                    "low": "9.16",
                    "close": "9.29",
                    "pre_close": "9.21",
                    "vol": "100",
                    "amount": "123.4",
                }
            ]
        )

    def adj_factor(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "600000.SH", "adj_factor": "1.25"}])

    def stk_limit(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "600000.SH", "up_limit": "10.13", "down_limit": "8.29"}])

    def suspend_d(self, **kwargs) -> pd.DataFrame:
        if self.fail_method == "suspend_d":
            raise PermissionError("suspension endpoint denied")
        self.suspend_call = kwargs
        return pd.DataFrame([{"ts_code": "600000.SH"}])

    def stock_st(self, **kwargs) -> pd.DataFrame:
        if self.fail_method == "stock_st":
            raise PermissionError("ST endpoint denied")
        self.stock_st_call = kwargs
        return pd.DataFrame([{"ts_code": "600000.SH"}])


class EmptyTusharePro(FakeTusharePro):
    def stock_basic(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def trade_cal(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def daily(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame()


class EmptyBaoStock(FakeBaoStock):
    def query_stock_basic(self):
        return Result(pd.DataFrame())

    def query_trade_dates(self, start_date, end_date):
        return Result(pd.DataFrame())


class FakeAkShareEastmoney:
    def stock_zh_a_spot_em(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "代码": "600000",
                    "最新价": "9.29",
                    "今开": "9.20",
                    "最高": "9.38",
                    "最低": "9.16",
                    "昨收": "9.21",
                    "成交量": "100",
                    "成交额": "123400",
                }
            ]
        )


def _tushare_provider(client: FakeTusharePro) -> TushareProvider:
    provider = TushareProvider.__new__(TushareProvider)
    provider.pro = client
    return provider


def test_all_market_providers_emit_shared_versioned_contracts_offline():
    baostock = BaoStockMarketDataProvider(FakeBaoStock())
    SECURITY_SCHEMA.validate(baostock.fetch_securities())
    TRADE_CALENDAR_SCHEMA.validate(baostock.fetch_trade_calendar(date(2026, 8, 8), date(2026, 8, 10)))
    DAILY_BAR_SCHEMA.validate(baostock.fetch_daily(date(2026, 8, 7)))

    tushare = _tushare_provider(FakeTusharePro())
    SECURITY_SCHEMA.validate(tushare.fetch_securities())
    TRADE_CALENDAR_SCHEMA.validate(tushare.fetch_trade_calendar(date(2026, 8, 9), date(2026, 8, 10)))
    DAILY_BAR_SCHEMA.validate(tushare.fetch_daily(date(2026, 8, 10)))

    akshare = AkShareLiveQuoteProvider(FakeAkShareQuotes())
    LIVE_QUOTE_SCHEMA.validate(akshare.fetch_quotes(["600000.SH"]))


def test_tushare_contract_preserves_units_calendar_state_and_historical_safety_flags():
    client = FakeTusharePro()
    provider = _tushare_provider(client)

    calendar = provider.fetch_trade_calendar(date(2026, 8, 9), date(2026, 8, 10))
    bars = provider.fetch_daily(date(2026, 8, 10))

    assert calendar["is_open"].tolist() == [False, True]
    assert bars.iloc[0]["volume"] == 10_000.0
    assert bars.iloc[0]["amount"] == 123_400.0
    assert bool(bars.iloc[0]["suspended"])
    assert bool(bars.iloc[0]["is_st"])
    assert client.suspend_call == {"suspend_type": "S", "trade_date": "20260810"}
    assert client.stock_st_call == {"trade_date": "20260810"}


def test_provider_empty_results_keep_canonical_contract_columns():
    baostock = BaoStockMarketDataProvider(EmptyBaoStock())
    tushare = _tushare_provider(EmptyTusharePro())

    cases = [
        (SECURITY_SCHEMA, baostock.fetch_securities()),
        (TRADE_CALENDAR_SCHEMA, baostock.fetch_trade_calendar(date(2026, 8, 9), date(2026, 8, 10))),
        (DAILY_BAR_SCHEMA, baostock.fetch_daily(date(2026, 8, 10))),
        (SECURITY_SCHEMA, tushare.fetch_securities()),
        (TRADE_CALENDAR_SCHEMA, tushare.fetch_trade_calendar(date(2026, 8, 9), date(2026, 8, 10))),
        (DAILY_BAR_SCHEMA, tushare.fetch_daily(date(2026, 8, 10))),
    ]

    for schema, frame in cases:
        assert frame.empty
        assert tuple(frame.columns) == schema.columns


def test_akshare_eastmoney_contract_converts_lots_to_shares():
    quotes = AkShareLiveQuoteProvider(FakeAkShareEastmoney()).fetch_quotes(["600000.SH"])

    assert LIVE_QUOTE_SCHEMA.validate(quotes).row_count == 1
    assert quotes.iloc[0]["volume"] == 10_000.0
    assert quotes.iloc[0]["amount"] == 123_400.0


def test_tushare_daily_rejects_missing_adjustment_factor_instead_of_inventing_one():
    client = FakeTusharePro()
    client.adj_factor = lambda **_kwargs: pd.DataFrame(columns=["ts_code", "adj_factor"])

    with pytest.raises(MarketDataValidationError, match="adj_factor is not nullable"):
        _tushare_provider(client).fetch_daily(date(2026, 8, 10))


def test_tushare_daily_rejects_missing_volume_instead_of_inventing_zero():
    client = FakeTusharePro()
    client.daily = lambda **_kwargs: FakeTusharePro().daily().assign(vol=[None])

    with pytest.raises(MarketDataValidationError, match="volume is not nullable"):
        _tushare_provider(client).fetch_daily(date(2026, 8, 10))


def test_tushare_daily_allows_explicitly_unavailable_price_limits():
    client = FakeTusharePro()
    client.stk_limit = lambda **_kwargs: pd.DataFrame()

    bars = _tushare_provider(client).fetch_daily(date(2026, 8, 10))

    assert pd.isna(bars.iloc[0]["up_limit"])
    assert pd.isna(bars.iloc[0]["down_limit"])


def test_baostock_empty_daily_normalizer_uses_canonical_contract():
    frame = BaoStockMarketDataProvider._normalize_daily(pd.DataFrame())

    assert frame.empty
    assert tuple(frame.columns) == DAILY_BAR_SCHEMA.columns


@pytest.mark.parametrize("endpoint", ["suspend_d", "stock_st"])
def test_tushare_daily_fails_closed_when_historical_safety_state_is_unavailable(endpoint):
    provider = _tushare_provider(FakeTusharePro(fail_method=endpoint))

    with pytest.raises(PermissionError, match="denied"):
        provider.fetch_daily(date(2026, 8, 10))


def test_workflow_rejects_invalid_eod_provider_batch_before_persistence(settings):
    at = datetime(2026, 8, 10, 16, 0, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    invalid_securities = make_securities([(code, "公司")]).assign(raw_provider_field="leak")
    database = Database(settings.database_path, settings.initial_cash)
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeRangeMarket(invalid_securities, make_daily([code], at, days=3)),
        live_provider=FakeLive(pd.DataFrame()),
        news_provider=FakeMarket(),
        llm_provider=FakeLLM(),
    )

    with pytest.raises(MarketDataValidationError, match="unexpected columns"):
        workflow.sync_eod(date(2026, 8, 5), date(2026, 8, 10))

    assert database.get_securities().empty
    assert database.get_recent_daily_bars().empty
    job = database.latest_job("sync-eod")
    assert job and job["status"] == "failed"
    assert "unexpected columns" in job["message"]


def test_workflow_rejects_invalid_live_quote_before_persistence_and_order_planning(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}]
        )
    )
    database.upsert_securities(make_securities([(code, "公司")]))
    database.upsert_daily_bars(make_daily([code], at))
    invalid_quotes = make_quotes([code], at).drop(columns=["source"])
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(invalid_quotes),
        llm_provider=FakeLLM(),
    )

    with pytest.raises(MarketDataValidationError, match="missing columns"):
        workflow.run_preclose(as_of=at)

    assert database.get_latest_quotes().empty
    assert database.proposed_orders().empty
    job = database.latest_job("run-preclose")
    assert job and job["status"] == "failed"
    assert "missing columns" in job["message"]
