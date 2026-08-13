from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd


def _to_ts_code(code: str) -> str:
    exchange, symbol = code.lower().split(".", 1)
    return f"{symbol}.{exchange.upper()}"


def _to_baostock_code(ts_code: str) -> str:
    symbol, exchange = ts_code.split(".", 1)
    return f"{exchange.lower()}.{symbol}"


def _is_main_board(ts_code: str) -> bool:
    symbol, exchange = ts_code.split(".", 1)
    if exchange == "SH":
        return symbol.startswith(("600", "601", "603", "605"))
    if exchange == "SZ":
        return symbol.startswith(("000", "001", "002", "003"))
    return False


class BaoStockMarketDataProvider:
    """Free learning-data adapter for history, calendars and dated ST/trading state."""

    source_name = "baostock"

    def __init__(self, client: Any | None = None):
        if client is None:
            import baostock as client

        self.client = client

    @contextmanager
    def _session(self) -> Iterator[Any]:
        login = self.client.login()
        if str(login.error_code) != "0":
            raise RuntimeError(f"BaoStock login failed [{login.error_code}]: {login.error_msg}")
        try:
            yield self.client
        finally:
            self.client.logout()

    @staticmethod
    def _frame(result: Any, operation: str) -> pd.DataFrame:
        if str(result.error_code) != "0":
            raise RuntimeError(f"BaoStock {operation} failed [{result.error_code}]: {result.error_msg}")

        # BaoStock's ResultData.get_data() still calls DataFrame.append(), which
        # was removed in pandas 2.0. Consume its public row cursor instead so the
        # adapter remains compatible with current pandas and still follows
        # BaoStock's built-in pagination.
        if all(hasattr(result, attribute) for attribute in ("fields", "next", "get_row_data")):
            rows: list[list[Any]] = []
            while str(result.error_code) == "0" and result.next():
                rows.append(result.get_row_data())
            if str(result.error_code) != "0":
                raise RuntimeError(f"BaoStock {operation} failed [{result.error_code}]: {result.error_msg}")
            return pd.DataFrame(rows, columns=list(result.fields))

        # Keep lightweight test doubles and compatible alternative clients
        # working when they expose only the conventional get_data() method.
        return result.get_data()

    def fetch_securities(self) -> pd.DataFrame:
        with self._session() as client:
            raw = self._frame(client.query_stock_basic(), "query_stock_basic")
        if raw.empty:
            return pd.DataFrame()
        frame = raw[raw["type"].astype(str) == "1"].copy()
        frame["ts_code"] = frame["code"].astype(str).map(_to_ts_code)
        frame["symbol"] = frame["ts_code"].str.split(".").str[0]
        frame["exchange"] = frame["ts_code"].str.split(".").str[1]
        frame["name"] = frame["code_name"].astype(str)
        frame["list_date"] = pd.to_datetime(frame["ipoDate"], errors="coerce").dt.date
        frame["delist_date"] = pd.to_datetime(frame["outDate"], errors="coerce").dt.date
        frame["list_status"] = np.where(frame["status"].astype(str) == "1", "L", "D")
        frame["market"] = np.where(frame["ts_code"].map(_is_main_board), "主板", "其他")
        frame = frame.dropna(subset=["list_date"])
        return frame[
            ["ts_code", "symbol", "name", "exchange", "market", "list_date", "delist_date", "list_status"]
        ].reset_index(drop=True)

    def fetch_trade_calendar(self, start: date, end: date) -> pd.DataFrame:
        expanded_start = start - timedelta(days=31)
        with self._session() as client:
            raw = self._frame(
                client.query_trade_dates(start_date=expanded_start.isoformat(), end_date=end.isoformat()),
                "query_trade_dates",
            )
        if raw.empty:
            return pd.DataFrame(columns=["cal_date", "is_open", "pretrade_date"])
        raw["cal_date"] = pd.to_datetime(raw["calendar_date"], errors="coerce").dt.date
        raw["is_open"] = raw["is_trading_day"].astype(str) == "1"
        previous_open: date | None = None
        previous_dates: list[date | None] = []
        for row in raw.sort_values("cal_date").to_dict("records"):
            previous_dates.append(previous_open)
            if bool(row["is_open"]):
                previous_open = row["cal_date"]
        raw = raw.sort_values("cal_date").copy()
        raw["pretrade_date"] = previous_dates
        raw = raw[(raw["cal_date"] >= start) & (raw["cal_date"] <= end)]
        return raw[["cal_date", "is_open", "pretrade_date"]].reset_index(drop=True)

    @staticmethod
    def _normalize_daily(raw: pd.DataFrame) -> pd.DataFrame:
        if raw.empty:
            return pd.DataFrame()
        frame = raw.copy()
        frame["ts_code"] = frame["code"].astype(str).map(_to_ts_code)
        frame["trade_date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        numeric = ["open", "high", "low", "close", "preclose", "volume", "amount"]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["suspended"] = frame["tradestatus"].astype(str) != "1"
        frame["is_st"] = frame["isST"].astype(str) == "1"
        frame["close"] = frame["close"].fillna(frame["preclose"])
        for column in ("open", "high", "low"):
            frame[column] = frame[column].fillna(frame["close"])
        frame["volume"] = frame["volume"].fillna(0.0)
        frame["amount"] = frame["amount"].fillna(0.0)
        frame = frame.dropna(subset=["trade_date", "open", "high", "low", "close"])
        limit_rate = np.where(frame["is_st"], 0.05, 0.10)
        frame["up_limit"] = (frame["preclose"] * (1.0 + limit_rate)).round(2)
        frame["down_limit"] = (frame["preclose"] * (1.0 - limit_rate)).round(2)
        frame["adj_factor"] = 1.0
        return frame[
            [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "preclose",
                "volume",
                "amount",
                "adj_factor",
                "up_limit",
                "down_limit",
                "suspended",
                "is_st",
            ]
        ].rename(columns={"preclose": "pre_close"})

    def iter_daily_range(
        self,
        start: date,
        end: date,
        ts_codes: Sequence[str] | None = None,
    ) -> Iterator[pd.DataFrame]:
        codes = list(ts_codes) if ts_codes is not None else self.fetch_securities()["ts_code"].astype(str).tolist()
        codes = sorted(code for code in codes if _is_main_board(code))
        fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"
        batch: list[pd.DataFrame] = []
        with self._session() as client:
            for ts_code in codes:
                result = client.query_history_k_data_plus(
                    _to_baostock_code(ts_code),
                    fields,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                    frequency="d",
                    adjustflag="3",
                )
                raw = self._frame(result, f"query_history_k_data_plus({ts_code})")
                frame = self._normalize_daily(raw)
                if not frame.empty:
                    batch.append(frame)
                if len(batch) >= 100:
                    yield pd.concat(batch, ignore_index=True)
                    batch.clear()
            if batch:
                yield pd.concat(batch, ignore_index=True)

    def fetch_daily(self, trade_date: date) -> pd.DataFrame:
        frames = list(self.iter_daily_range(trade_date, trade_date))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
