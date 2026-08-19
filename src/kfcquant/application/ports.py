from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from kfcquant.ingestion import IngestionManifest
from kfcquant.models import (
    CandidateOutcome,
    LLMCallTrace,
    NewsDocument,
    OpportunityOutcome,
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskEvent,
    RiskExtractionResult,
)


@runtime_checkable
class MarketRepository(Protocol):
    def ingest_market_batch(self, frame: pd.DataFrame, manifest: IngestionManifest) -> None: ...

    def is_trading_day(self, value: date) -> bool: ...


@runtime_checkable
class ResearchRepository(Protocol):
    def is_trading_day(self, value: date) -> bool: ...

    def previous_trading_day(self, value: date) -> date | None: ...

    def trading_day_lookback(self, value: date, trading_days: int) -> date | None: ...

    def get_securities(self) -> pd.DataFrame: ...

    def get_recent_daily_bars(self, trading_days: int = 30, as_of: date | None = None) -> pd.DataFrame: ...

    def get_risk_events(self, start: datetime, end: datetime) -> pd.DataFrame: ...

    def unprocessed_official_codes(self, start: datetime, as_of: datetime) -> set[str]: ...

    def latest_signal_run(
        self,
        on_date: date | None = None,
        signal_kind: str | None = None,
        include_non_terminal: bool = False,
    ) -> dict[str, Any] | None: ...

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame: ...

    def get_open_positions(self) -> pd.DataFrame: ...

    def get_cash(self) -> float: ...


@runtime_checkable
class JobRepository(Protocol):
    def start_job(
        self,
        job_run_id: str,
        job_name: str,
        started_at: datetime,
        lease_duration: timedelta,
        scheduled_for: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]: ...

    def heartbeat_job(self, job_run_id: str, heartbeat_at: datetime, lease_duration: timedelta) -> bool: ...

    def finish_job(
        self,
        job_run_id: str,
        finished_at: datetime,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def recover_expired_jobs(self, recovered_at: datetime) -> list[str]: ...


@runtime_checkable
class NewsRepository(Protocol):
    def get_securities(self) -> pd.DataFrame: ...

    def save_news_documents(self, documents: list[NewsDocument]) -> int: ...

    def pending_news_documents(self, limit: int = 500) -> list[NewsDocument]: ...

    def mark_document(
        self, document_id: str, status: str, error: str | None = None, content: str | None = None
    ) -> None: ...

    def save_risk_events(self, events: list[RiskEvent]) -> None: ...

    def complete_risk_extraction(
        self,
        document_id: str,
        result: RiskExtractionResult,
        *,
        content: str | None = None,
    ) -> None: ...

    def fail_risk_extraction(
        self,
        document_id: str,
        trace: LLMCallTrace,
        *,
        content: str | None = None,
    ) -> None: ...


@runtime_checkable
class PortfolioRepository(Protocol):
    def get_open_positions(self) -> pd.DataFrame: ...

    def save_order(self, order: PaperOrder) -> bool: ...

    def proposed_orders(self, run_id: str | None = None) -> pd.DataFrame: ...

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame: ...

    def reject_order(self, order_id: str, reason: str) -> None: ...

    def get_quote_near(self, ts_code: str, at_or_before: datetime) -> dict[str, Any] | None: ...

    def get_cash(self) -> float: ...

    def apply_buy_fill(self, fill: PaperFill, position: PaperPosition) -> None: ...

    def create_sell_order_if_absent(self, order: PaperOrder) -> bool: ...

    def apply_sell_fill(self, fill: PaperFill, position_id: str, reason: str) -> PaperPosition: ...

    def count_trading_days(self, start: date, end: date) -> int: ...

    def save_outcome(self, outcome: OpportunityOutcome) -> None: ...

    def latest_signal_run(
        self,
        on_date: date | None = None,
        signal_kind: str | None = None,
        include_non_terminal: bool = False,
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class CandidateEvaluationRepository(Protocol):
    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame: ...

    def save_candidate_outcome(self, outcome: CandidateOutcome) -> None: ...


@runtime_checkable
class ReportRepository(Protocol):
    def save_report(
        self,
        report_id: str,
        report_date: date,
        generated_at: datetime,
        report_type: str,
        content: str,
        model_name: str,
    ) -> None: ...
