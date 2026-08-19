from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import pandas as pd

from kfcquant.application.queries import (
    DataHealthProjection,
    EvaluationProjection,
    PortfolioProjection,
    SignalProjection,
    TradingActivityProjection,
)
from kfcquant.db import Database
from kfcquant.models import SignalKind


class DuckDBDashboardQueryModel:
    """DuckDB-backed, read-only projections for presentation concerns."""

    def __init__(self, database: Database):
        self._database = database

    def latest_signal(self, signal_kind: SignalKind, on_date: date | None = None) -> SignalProjection | None:
        run = self._database.latest_signal_run(on_date, signal_kind.value)
        if run is None:
            return None
        candidates = self._database.get_candidates(str(run["run_id"]), include_blocked=True)
        return SignalProjection(run=run, candidates=candidates)

    def risk_events(self, event_ids: Sequence[str]) -> pd.DataFrame:
        selected = sorted({str(event_id) for event_id in event_ids})
        if not selected:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in selected)
        with self._database.connect(read_only=True) as connection:
            return connection.execute(
                f"SELECT * FROM risk_events WHERE event_id IN ({placeholders}) ORDER BY published_at DESC",
                selected,
            ).fetchdf()

    def portfolio(self) -> PortfolioProjection:
        positions = self._database.get_open_positions()
        if positions.empty:
            return PortfolioProjection(self._database.get_cash(), positions)
        quotes = self._database.get_latest_quotes()
        if quotes.empty:
            projected = positions.copy()
            projected["price"] = pd.NA
            projected["captured_at"] = pd.NaT
        else:
            projected = positions.merge(
                quotes[["ts_code", "price", "captured_at"]],
                on="ts_code",
                how="left",
            )
        projected["market_value"] = projected["shares"] * projected["price"]
        projected["unrealized_pnl"] = projected["shares"] * (projected["price"] - projected["cost_basis"])
        return PortfolioProjection(self._database.get_cash(), projected)

    def trading_activity(self, limit: int = 1000) -> TradingActivityProjection:
        return TradingActivityProjection(
            orders=self._database.table_with_strategy("paper_orders", limit),
            fills=self._database.table("paper_fills", limit),
        )

    def evaluations(self, limit: int = 1000) -> EvaluationProjection:
        return EvaluationProjection(
            morning_candidates=self._database.candidate_outcomes(SignalKind.MORNING_WATCHLIST.value).head(limit),
            preclose_candidates=self._database.candidate_outcomes(SignalKind.PRECLOSE_ENTRY.value).head(limit),
            opportunities=self._database.table_with_strategy("opportunity_outcomes", limit),
        )

    def data_health(self, limit: int = 200) -> DataHealthProjection:
        documents = self._database.table("news_documents", limit)
        if documents.empty:
            status_counts = pd.DataFrame(columns=["status", "count"])
        else:
            status_counts = (
                documents["processing_status"]
                .value_counts()
                .rename_axis("status")
                .reset_index(name="count")
            )
        return DataHealthProjection(
            jobs=self._database.table("job_runs", limit),
            runs=self._database.recent_signal_runs(limit),
            news_status_counts=status_counts,
        )

    def latest_report(self) -> Mapping[str, Any] | None:
        reports = self._database.table("reports", 60)
        if reports.empty:
            return None
        return reports.sort_values("generated_at", ascending=False).iloc[0].to_dict()
