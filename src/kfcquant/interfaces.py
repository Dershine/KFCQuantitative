from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

import pandas as pd

from kfcquant.models import IntradayBar, LLMCallTrace, NewsDocument, RiskExtractionResult


class LLMCallError(RuntimeError):
    """An LLM failure carrying safe, persistable call metadata."""

    def __init__(self, trace: LLMCallTrace):
        super().__init__(trace.error_message or trace.error_type or "LLM call failed")
        self.trace = trace


class MarketDataProvider(Protocol):
    source_name: str

    def fetch_securities(self) -> pd.DataFrame: ...

    def fetch_trade_calendar(self, start: date, end: date) -> pd.DataFrame: ...

    def fetch_daily(self, trade_date: date) -> pd.DataFrame: ...


class LiveQuoteProvider(Protocol):
    source_name: str

    def fetch_quotes(self, ts_codes: Sequence[str] | None = None) -> pd.DataFrame: ...

    def fetch_intraday_bars(
        self, ts_code: str, start: datetime, end: datetime, frequency_minutes: int = 5
    ) -> list[IntradayBar]: ...


class NewsProvider(Protocol):
    def fetch_official_documents(self, start: datetime, end: datetime) -> list[NewsDocument]: ...

    def fetch_mainstream_documents(self, start: datetime, end: datetime) -> list[NewsDocument]: ...


class LLMProvider(Protocol):
    def extract_risk_events(self, document: NewsDocument) -> RiskExtractionResult: ...

    def generate_report(self, context: dict[str, object]) -> str: ...
