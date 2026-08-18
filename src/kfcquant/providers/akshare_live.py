from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.market_data import LIVE_QUOTE_SCHEMA
from kfcquant.models import IntradayBar


def _to_ts_code(symbol: str) -> str:
    symbol = str(symbol).lower()
    if symbol.startswith(("sh", "sz", "bj")):
        exchange = symbol[:2].upper()
        return f"{symbol[2:].zfill(6)}.{exchange}"
    symbol = symbol.zfill(6)
    return f"{symbol}.SH" if symbol.startswith("6") else f"{symbol}.SZ"


class AkShareLiveQuoteProvider:
    """Best-effort prototype live quote adapter backed by AKShare."""

    source_name = "akshare-eastmoney"

    def __init__(self, client: Any | None = None):
        if client is None:
            import akshare as client

        self.client = client

    def fetch_quotes(self, ts_codes: Sequence[str] | None = None) -> pd.DataFrame:
        captured_at = datetime.now(SHANGHAI_TZ)
        try:
            raw = self.client.stock_zh_a_spot_em()
            frame = self._normalize_eastmoney(raw, captured_at)
            self.source_name = "akshare-eastmoney"
        except Exception as eastmoney_error:
            try:
                raw = self.client.stock_zh_a_spot()
                frame = self._normalize_sina(raw, captured_at)
                self.source_name = "akshare-sina"
            except Exception as sina_error:
                raise RuntimeError(
                    f"all AKShare quote sources failed; Eastmoney: {eastmoney_error}; Sina: {sina_error}"
                ) from sina_error
        if ts_codes:
            frame = frame[frame["ts_code"].isin(set(ts_codes))]
        return LIVE_QUOTE_SCHEMA.validate(frame.reset_index(drop=True)).frame

    @staticmethod
    def _normalize_eastmoney(raw: pd.DataFrame, captured_at: datetime) -> pd.DataFrame:
        rename = {
            "代码": "symbol",
            "最新价": "price",
            "今开": "open",
            "最高": "high",
            "最低": "low",
            "昨收": "pre_close",
            "成交量": "volume",
            "成交额": "amount",
        }
        missing = [column for column in rename if column not in raw.columns]
        if missing:
            raise RuntimeError(f"AKShare quote schema changed; missing columns: {missing}")
        frame = raw.rename(columns=rename)[list(rename.values())].copy()
        frame["ts_code"] = frame["symbol"].astype(str).map(_to_ts_code)
        numeric = ["price", "open", "high", "low", "pre_close", "volume", "amount"]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # Eastmoney spot volume is expressed in lots (100 shares).
        frame["volume"] = frame["volume"] * 100.0
        frame["captured_at"] = captured_at
        frame["source"] = "akshare-eastmoney"
        frame = frame.dropna(subset=["price", "open", "high", "low", "pre_close"])
        return frame[
            ["ts_code", "captured_at", "price", "open", "high", "low", "pre_close", "volume", "amount", "source"]
        ]

    @staticmethod
    def _normalize_sina(raw: pd.DataFrame, captured_at: datetime) -> pd.DataFrame:
        rename = {
            "代码": "symbol",
            "最新价": "price",
            "今开": "open",
            "最高": "high",
            "最低": "low",
            "昨收": "pre_close",
            "成交量": "volume",
            "成交额": "amount",
            "时间戳": "source_time",
        }
        missing = [column for column in rename if column not in raw.columns]
        if missing:
            raise RuntimeError(f"AKShare Sina quote schema changed; missing columns: {missing}")
        frame = raw.rename(columns=rename)[list(rename.values())].copy()
        frame["ts_code"] = frame["symbol"].astype(str).map(_to_ts_code)
        numeric = ["price", "open", "high", "low", "pre_close", "volume", "amount"]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        timestamps = pd.to_datetime(
            captured_at.strftime("%Y-%m-%d ") + frame["source_time"].astype(str), errors="coerce"
        )
        frame["captured_at"] = [
            value.to_pydatetime().replace(tzinfo=SHANGHAI_TZ) if not pd.isna(value) else captured_at
            for value in timestamps
        ]
        frame["source"] = "akshare-sina"
        frame = frame.dropna(subset=["price", "open", "high", "low", "pre_close", "volume", "amount"])
        return frame[
            ["ts_code", "captured_at", "price", "open", "high", "low", "pre_close", "volume", "amount", "source"]
        ]

    def fetch_intraday_bars(
        self, ts_code: str, start: datetime, end: datetime, frequency_minutes: int = 5
    ) -> list[IntradayBar]:
        if frequency_minutes not in (1, 5, 15, 30, 60):
            raise ValueError("unsupported intraday frequency")
        symbol = ts_code.split(".")[0]
        try:
            frame = self.client.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                period=str(frequency_minutes),
                adjust="",
            )
            return self._eastmoney_intraday(ts_code, frame, frequency_minutes)
        except Exception as eastmoney_error:
            try:
                exchange = ts_code.split(".")[1].lower()
                frame = self.client.stock_zh_a_minute(
                    symbol=f"{exchange}{symbol}", period=str(frequency_minutes), adjust=""
                )
                return self._sina_intraday(ts_code, frame, start, end, frequency_minutes)
            except Exception as sina_error:
                raise RuntimeError(
                    f"all AKShare minute sources failed; Eastmoney: {eastmoney_error}; Sina: {sina_error}"
                ) from sina_error

    @staticmethod
    def _eastmoney_intraday(ts_code: str, frame: pd.DataFrame, frequency_minutes: int) -> list[IntradayBar]:
        if frame.empty:
            return []
        bars: list[IntradayBar] = []
        for row in frame.to_dict("records"):
            bar_end = pd.to_datetime(row["时间"]).to_pydatetime().replace(tzinfo=SHANGHAI_TZ)
            bars.append(
                IntradayBar(
                    ts_code=ts_code,
                    start_at=bar_end - pd.Timedelta(minutes=frequency_minutes),
                    end_at=bar_end,
                    open=float(row["开盘"]),
                    high=float(row["最高"]),
                    low=float(row["最低"]),
                    close=float(row["收盘"]),
                    volume=float(row["成交量"]) * 100.0,
                    amount=float(row["成交额"]),
                    source="akshare-eastmoney",
                )
            )
        return bars

    @staticmethod
    def _sina_intraday(
        ts_code: str,
        frame: pd.DataFrame,
        start: datetime,
        end: datetime,
        frequency_minutes: int,
    ) -> list[IntradayBar]:
        required = {"day", "open", "high", "low", "close", "volume", "amount"}
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(f"AKShare Sina minute schema changed; missing columns: {sorted(missing)}")
        bars: list[IntradayBar] = []
        for row in frame.to_dict("records"):
            bar_end = pd.to_datetime(row["day"]).to_pydatetime().replace(tzinfo=SHANGHAI_TZ)
            if not (start <= bar_end <= end):
                continue
            bars.append(
                IntradayBar(
                    ts_code=ts_code,
                    start_at=bar_end - pd.Timedelta(minutes=frequency_minutes),
                    end_at=bar_end,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    amount=float(row["amount"]),
                    source="akshare-sina",
                )
            )
        return bars
