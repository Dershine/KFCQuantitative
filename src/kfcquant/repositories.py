from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from kfcquant.application.ports import (
    CandidateEvaluationRepository,
    JobRepository,
    MarketRepository,
    NewsRepository,
    PortfolioRepository,
    ReportRepository,
    ResearchRepository,
)
from kfcquant.db import Database
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


class DuckDBMarketRepository:
    def __init__(self, database: Database):
        self._database = database

    def ingest_market_batch(self, frame: pd.DataFrame, manifest: IngestionManifest) -> None:
        self._database.ingest_market_batch(frame, manifest)

    def is_trading_day(self, value: date) -> bool:
        return self._database.is_trading_day(value)


class DuckDBResearchRepository:
    def __init__(self, database: Database):
        self._database = database

    def is_trading_day(self, value: date) -> bool:
        return self._database.is_trading_day(value)

    def previous_trading_day(self, value: date) -> date | None:
        return self._database.previous_trading_day(value)

    def trading_day_lookback(self, value: date, trading_days: int) -> date | None:
        return self._database.trading_day_lookback(value, trading_days)

    def get_securities(self) -> pd.DataFrame:
        return self._database.get_securities()

    def get_recent_daily_bars(self, trading_days: int = 30, as_of: date | None = None) -> pd.DataFrame:
        return self._database.get_recent_daily_bars(trading_days, as_of)

    def get_risk_events(self, start: datetime, end: datetime) -> pd.DataFrame:
        return self._database.get_risk_events(start, end)

    def unprocessed_official_codes(self, start: datetime, as_of: datetime) -> set[str]:
        return self._database.unprocessed_official_codes(start, as_of)

    def latest_signal_run(
        self,
        on_date: date | None = None,
        signal_kind: str | None = None,
        include_non_terminal: bool = False,
    ) -> dict[str, Any] | None:
        return self._database.latest_signal_run(on_date, signal_kind, include_non_terminal)

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame:
        return self._database.get_candidates(run_id, include_blocked)

    def get_open_positions(self) -> pd.DataFrame:
        return self._database.get_open_positions()

    def get_cash(self) -> float:
        return self._database.get_cash()


class DuckDBJobRepository:
    def __init__(self, database: Database):
        self._database = database

    def start_job(
        self,
        job_run_id: str,
        job_name: str,
        started_at: datetime,
        lease_duration: timedelta,
        scheduled_for: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        return self._database.start_job(
            job_run_id, job_name, started_at, lease_duration, scheduled_for, metadata
        )

    def heartbeat_job(self, job_run_id: str, heartbeat_at: datetime, lease_duration: timedelta) -> bool:
        return self._database.heartbeat_job(job_run_id, heartbeat_at, lease_duration)

    def finish_job(
        self,
        job_run_id: str,
        finished_at: datetime,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._database.finish_job(job_run_id, finished_at, status, message, metadata)

    def recover_expired_jobs(self, recovered_at: datetime) -> list[str]:
        return self._database.recover_expired_jobs(recovered_at)


class DuckDBNewsRepository:
    def __init__(self, database: Database):
        self._database = database

    def get_securities(self) -> pd.DataFrame:
        return self._database.get_securities()

    def save_news_documents(self, documents: list[NewsDocument]) -> int:
        return self._database.save_news_documents(documents)

    def pending_news_documents(self, limit: int = 500) -> list[NewsDocument]:
        return self._database.pending_news_documents(limit)

    def mark_document(
        self, document_id: str, status: str, error: str | None = None, content: str | None = None
    ) -> None:
        self._database.mark_document(document_id, status, error, content)

    def save_risk_events(self, events: list[RiskEvent]) -> None:
        self._database.save_risk_events(events)

    def complete_risk_extraction(
        self,
        document_id: str,
        result: RiskExtractionResult,
        *,
        content: str | None = None,
    ) -> None:
        self._database.complete_risk_extraction(document_id, result, content=content)

    def fail_risk_extraction(
        self,
        document_id: str,
        trace: LLMCallTrace,
        *,
        content: str | None = None,
    ) -> None:
        self._database.fail_risk_extraction(document_id, trace, content=content)


class DuckDBPortfolioRepository:
    def __init__(self, database: Database):
        self._database = database

    def get_open_positions(self) -> pd.DataFrame:
        return self._database.get_open_positions()

    def save_order(self, order: PaperOrder) -> bool:
        return self._database.save_order(order)

    def proposed_orders(self, run_id: str | None = None) -> pd.DataFrame:
        return self._database.proposed_orders(run_id)

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame:
        return self._database.get_candidates(run_id, include_blocked)

    def reject_order(self, order_id: str, reason: str) -> None:
        self._database.reject_order(order_id, reason)

    def get_quote_near(self, ts_code: str, at_or_before: datetime) -> dict[str, Any] | None:
        return self._database.get_quote_near(ts_code, at_or_before)

    def get_cash(self) -> float:
        return self._database.get_cash()

    def apply_buy_fill(self, fill: PaperFill, position: PaperPosition) -> None:
        self._database.apply_buy_fill(fill, position)

    def create_sell_order_if_absent(self, order: PaperOrder) -> bool:
        return self._database.create_sell_order_if_absent(order)

    def apply_sell_fill(self, fill: PaperFill, position_id: str, reason: str) -> PaperPosition:
        return self._database.apply_sell_fill(fill, position_id, reason)

    def count_trading_days(self, start: date, end: date) -> int:
        return self._database.count_trading_days(start, end)

    def save_outcome(self, outcome: OpportunityOutcome) -> None:
        self._database.save_outcome(outcome)

    def latest_signal_run(
        self,
        on_date: date | None = None,
        signal_kind: str | None = None,
        include_non_terminal: bool = False,
    ) -> dict[str, Any] | None:
        return self._database.latest_signal_run(on_date, signal_kind, include_non_terminal)


class DuckDBCandidateEvaluationRepository:
    def __init__(self, database: Database):
        self._database = database

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame:
        return self._database.get_candidates(run_id, include_blocked)

    def save_candidate_outcome(self, outcome: CandidateOutcome) -> None:
        self._database.save_candidate_outcome(outcome)


class DuckDBReportRepository:
    def __init__(self, database: Database):
        self._database = database

    def save_report(
        self,
        report_id: str,
        report_date: date,
        generated_at: datetime,
        report_type: str,
        content: str,
        model_name: str,
    ) -> None:
        self._database.save_report(report_id, report_date, generated_at, report_type, content, model_name)


@dataclass(frozen=True)
class DuckDBRepositories:
    market: MarketRepository
    research: ResearchRepository
    jobs: JobRepository
    news: NewsRepository
    portfolio: PortfolioRepository
    evaluation: CandidateEvaluationRepository
    report: ReportRepository

    def __init__(self, database: Database):
        object.__setattr__(self, "market", DuckDBMarketRepository(database))
        object.__setattr__(self, "research", DuckDBResearchRepository(database))
        object.__setattr__(self, "jobs", DuckDBJobRepository(database))
        object.__setattr__(self, "news", DuckDBNewsRepository(database))
        object.__setattr__(self, "portfolio", DuckDBPortfolioRepository(database))
        object.__setattr__(self, "evaluation", DuckDBCandidateEvaluationRepository(database))
        object.__setattr__(self, "report", DuckDBReportRepository(database))
