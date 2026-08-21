from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from kfcquant.models import SignalKind


@dataclass(frozen=True)
class SignalProjection:
    run: Mapping[str, Any]
    candidates: pd.DataFrame


@dataclass(frozen=True)
class PortfolioProjection:
    cash: float
    positions: pd.DataFrame


@dataclass(frozen=True)
class TradingActivityProjection:
    orders: pd.DataFrame
    fills: pd.DataFrame


@dataclass(frozen=True)
class EvaluationProjection:
    morning_candidates: pd.DataFrame
    preclose_candidates: pd.DataFrame
    opportunities: pd.DataFrame


@dataclass(frozen=True)
class DataHealthProjection:
    jobs: pd.DataFrame
    runs: pd.DataFrame
    news_status_counts: pd.DataFrame


@runtime_checkable
class DashboardQueryModel(Protocol):
    """Stable read-only projections consumed by the Research Dashboard."""

    def latest_signal(self, signal_kind: SignalKind, on_date: date | None = None) -> SignalProjection | None: ...

    def latest_job(self, job_name: str, on_date: date | None = None) -> Mapping[str, Any] | None: ...

    def risk_events(self, event_ids: Sequence[str]) -> pd.DataFrame: ...

    def portfolio(self) -> PortfolioProjection: ...

    def trading_activity(self, limit: int = 1000) -> TradingActivityProjection: ...

    def evaluations(self, limit: int = 1000) -> EvaluationProjection: ...

    def data_health(self, limit: int = 200) -> DataHealthProjection: ...

    def latest_report(self) -> Mapping[str, Any] | None: ...
