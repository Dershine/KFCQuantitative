from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.providers.akshare_live import AkShareLiveQuoteProvider
from kfcquant.providers.akshare_news import AkShareNewsProvider
from kfcquant.providers.baostock_market import BaoStockMarketDataProvider
from kfcquant.providers.factory import build_market_provider, build_news_provider


class Result:
    error_code = "0"
    error_msg = "success"

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_data(self):
        return self.frame.copy()


class CursorResult:
    def __init__(self, fields, pages, error_on_page=None):
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = fields
        self._pages = pages
        self._error_on_page = error_on_page
        self._page_index = 0
        self._row_index = 0

    def next(self):
        if self._row_index < len(self._pages[self._page_index]):
            return True
        next_page = self._page_index + 1
        if self._error_on_page == next_page:
            self.error_code = "1001"
            self.error_msg = "page failed"
            return False
        if next_page >= len(self._pages):
            return False
        self._page_index = next_page
        self._row_index = 0
        return bool(self._pages[self._page_index])

    def get_row_data(self):
        row = self._pages[self._page_index][self._row_index]
        self._row_index += 1
        return row

    def get_data(self):
        raise AssertionError("BaoStock's pandas-incompatible get_data() must not be called")


class FakeBaoStock:
    def __init__(self):
        self.logout_count = 0

    def login(self):
        return SimpleNamespace(error_code="0", error_msg="success")

    def logout(self):
        self.logout_count += 1

    def query_stock_basic(self):
        return Result(
            pd.DataFrame(
                [
                    {
                        "code": "sh.600000",
                        "code_name": "浦发银行",
                        "ipoDate": "1999-11-10",
                        "outDate": "",
                        "type": "1",
                        "status": "1",
                    },
                    {
                        "code": "sh.000001",
                        "code_name": "上证指数",
                        "ipoDate": "1991-07-15",
                        "outDate": "",
                        "type": "2",
                        "status": "1",
                    },
                ]
            )
        )

    def query_trade_dates(self, start_date, end_date):
        return Result(
            pd.DataFrame(
                [
                    {"calendar_date": "2026-08-07", "is_trading_day": "1"},
                    {"calendar_date": "2026-08-08", "is_trading_day": "0"},
                    {"calendar_date": "2026-08-10", "is_trading_day": "1"},
                ]
            )
        )

    def query_history_k_data_plus(self, code, fields, start_date, end_date, frequency, adjustflag):
        return Result(
            pd.DataFrame(
                [
                    {
                        "date": "2026-08-07",
                        "code": code,
                        "open": "9.20",
                        "high": "9.30",
                        "low": "9.10",
                        "close": "9.21",
                        "preclose": "9.10",
                        "volume": "100000",
                        "amount": "921000",
                        "adjustflag": "3",
                        "tradestatus": "1",
                        "isST": "1",
                    }
                ]
            )
        )


class FakeAkShareNews:
    def stock_notice_report(self, symbol, date):
        return pd.DataFrame(
            [
                {
                    "代码": "600000",
                    "名称": "浦发银行",
                    "公告标题": "风险提示公告",
                    "公告类型": "风险提示",
                    "公告日期": pd.Timestamp(date).date(),
                    "网址": "https://example.test/notice",
                },
                {
                    "代码": "300001",
                    "名称": "创业板公司",
                    "公告标题": "公告",
                    "公告类型": "其他",
                    "公告日期": pd.Timestamp(date).date(),
                    "网址": "https://example.test/gem",
                },
            ]
        )

    def stock_info_global_cls(self, symbol):
        return pd.DataFrame(
            [
                {
                    "标题": "",
                    "内容": "浦发银行发布经营信息。",
                    "发布日期": date(2026, 8, 10),
                    "发布时间": time(13, 0),
                }
            ]
        )

    def stock_info_global_sina(self):
        raise RuntimeError("fixture outage")


class FakeAkShareQuotes:
    def stock_zh_a_spot_em(self):
        raise RuntimeError("eastmoney unavailable")

    def stock_zh_a_spot(self):
        return pd.DataFrame(
            [
                {
                    "代码": "sh600000",
                    "名称": "浦发银行",
                    "最新价": 9.29,
                    "涨跌额": 0.08,
                    "涨跌幅": 0.8,
                    "买入": 9.28,
                    "卖出": 9.29,
                    "昨收": 9.21,
                    "今开": 9.20,
                    "最高": 9.38,
                    "最低": 9.16,
                    "成交量": 62_542_539,
                    "成交额": 581_544_471,
                    "时间戳": "14:40:01",
                }
            ]
        )

    def stock_zh_a_hist_min_em(self, **kwargs):
        raise RuntimeError("eastmoney minute unavailable")

    def stock_zh_a_minute(self, symbol, period, adjust):
        return pd.DataFrame(
            [
                {
                    "day": "2026-08-10 14:40:00",
                    "open": 9.20,
                    "high": 9.30,
                    "low": 9.18,
                    "close": 9.29,
                    "volume": 100_000,
                    "amount": 925_000,
                }
            ]
        )


def test_baostock_normalizes_security_calendar_and_historical_state():
    client = FakeBaoStock()
    provider = BaoStockMarketDataProvider(client)
    securities = provider.fetch_securities()
    assert securities["ts_code"].tolist() == ["600000.SH"]
    calendar = provider.fetch_trade_calendar(date(2026, 8, 8), date(2026, 8, 10))
    assert calendar.iloc[-1]["pretrade_date"] == date(2026, 8, 7)
    bars = list(provider.iter_daily_range(date(2026, 8, 7), date(2026, 8, 10), ["600000.SH"]))[0]
    assert bool(bars.iloc[0]["is_st"])
    assert bars.iloc[0]["up_limit"] == 9.56
    assert client.logout_count == 3


def test_baostock_frame_uses_cursor_across_pages_without_dataframe_append():
    result = CursorResult(
        ["code", "name"],
        [
            [["sh.600000", "浦发银行"]],
            [["sz.000001", "平安银行"]],
        ],
    )

    frame = BaoStockMarketDataProvider._frame(result, "cursor-test")

    assert frame.to_dict("records") == [
        {"code": "sh.600000", "name": "浦发银行"},
        {"code": "sz.000001", "name": "平安银行"},
    ]


def test_baostock_frame_rejects_partial_data_when_a_later_page_fails():
    result = CursorResult(["code"], [[["sh.600000"]], [["sz.000001"]]], error_on_page=1)

    with pytest.raises(RuntimeError, match=r"BaoStock cursor-test failed \[1001\]: page failed"):
        BaoStockMarketDataProvider._frame(result, "cursor-test")


def test_baostock_frame_preserves_columns_for_an_empty_cursor_result():
    result = CursorResult(["code", "name"], [[]])

    frame = BaoStockMarketDataProvider._frame(result, "cursor-test")

    assert frame.empty
    assert frame.columns.tolist() == ["code", "name"]


def test_akshare_announcement_time_never_leaks_into_preclose():
    provider = AkShareNewsProvider(FakeAkShareNews())
    preclose = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    assert provider.fetch_official_documents(preclose.replace(hour=0), preclose) == []
    postclose = preclose.replace(hour=20, minute=30)
    documents = provider.fetch_official_documents(postclose.replace(hour=0), postclose)
    assert len(documents) == 1
    assert documents[0].published_at.time() == time(15, 1)
    assert documents[0].source == "akshare-eastmoney-announcement-mirror"


def test_akshare_news_tolerates_one_public_source_outage():
    provider = AkShareNewsProvider(FakeAkShareNews())
    documents = provider.fetch_mainstream_documents(
        datetime(2026, 8, 10, 0, 0, tzinfo=SHANGHAI_TZ),
        datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ),
    )
    assert len(documents) == 1
    assert documents[0].source == "akshare-cls"


def test_live_quotes_fall_back_to_sina_without_lot_conversion():
    provider = AkShareLiveQuoteProvider(FakeAkShareQuotes())
    quotes = provider.fetch_quotes(["600000.SH"])
    assert quotes.iloc[0]["source"] == "akshare-sina"
    assert quotes.iloc[0]["volume"] == 62_542_539
    assert quotes.iloc[0]["captured_at"].time() == time(14, 40, 1)


def test_intraday_bars_fall_back_to_sina():
    provider = AkShareLiveQuoteProvider(FakeAkShareQuotes())
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    bars = provider.fetch_intraday_bars("600000.SH", at.replace(hour=9, minute=30), at, 5)
    assert len(bars) == 1
    assert bars[0].source == "akshare-sina"
    assert bars[0].start_at.time() == time(14, 35)


def test_learning_profile_builds_free_providers(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db.duckdb",
        raw_data_dir=tmp_path / "raw",
        report_dir=tmp_path / "reports",
    )
    assert isinstance(build_market_provider(settings), BaoStockMarketDataProvider)
    assert isinstance(build_news_provider(settings), AkShareNewsProvider)
