from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from filelock import FileLock

from kfcquant.migrations import Migration, MigrationRunner
from kfcquant.models import (
    READABLE_RESEARCH_RUN_STATES,
    CandidateOutcome,
    CandidateScore,
    NewsDocument,
    OpportunityOutcome,
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskEvent,
    SignalRun,
    StrategyAttribution,
)
from kfcquant.strategy_identity import StrategyParameterSnapshot, canonical_parameter_json, parameter_hash

LOGGER = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = frozenset({"success", "degraded", "failed", "missed"})
LEGACY_PARAMETER_SNAPSHOT = StrategyParameterSnapshot.from_mapping({"legacy_unversioned": True})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS securities (
    ts_code VARCHAR PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    exchange VARCHAR NOT NULL,
    market VARCHAR,
    list_date DATE NOT NULL,
    delist_date DATE,
    list_status VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date DATE PRIMARY KEY,
    is_open BOOLEAN NOT NULL,
    pretrade_date DATE
);

CREATE TABLE IF NOT EXISTS daily_bars (
    ts_code VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    pre_close DOUBLE,
    volume DOUBLE NOT NULL,
    amount DOUBLE NOT NULL,
    adj_factor DOUBLE NOT NULL DEFAULT 1,
    up_limit DOUBLE,
    down_limit DOUBLE,
    suspended BOOLEAN NOT NULL DEFAULT false,
    is_st BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (ts_code, trade_date)
);

CREATE TABLE IF NOT EXISTS live_quotes (
    ts_code VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    price DOUBLE NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    pre_close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    amount DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    PRIMARY KEY (ts_code, captured_at)
);

CREATE TABLE IF NOT EXISTS news_documents (
    document_id VARCHAR PRIMARY KEY,
    ts_code VARCHAR,
    title VARCHAR NOT NULL,
    content VARCHAR,
    published_at TIMESTAMPTZ NOT NULL,
    source VARCHAR NOT NULL,
    source_tier VARCHAR NOT NULL,
    url VARCHAR,
    content_hash VARCHAR NOT NULL UNIQUE,
    fetched_at TIMESTAMPTZ NOT NULL,
    processing_status VARCHAR NOT NULL,
    processing_error VARCHAR
);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL,
    ts_code VARCHAR,
    event_type VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    hard_block BOOLEAN NOT NULL,
    evidence VARCHAR NOT NULL,
    source_url VARCHAR,
    published_at TIMESTAMPTZ NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL,
    model_name VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_runs (
    run_id VARCHAR PRIMARY KEY,
    as_of TIMESTAMPTZ NOT NULL,
    signal_kind VARCHAR NOT NULL DEFAULT 'preclose_entry',
    strategy_version VARCHAR NOT NULL DEFAULT 'preclose-v1',
    information_cutoff TIMESTAMPTZ,
    data_as_of TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    lifecycle_state VARCHAR NOT NULL DEFAULT 'published',
    data_fresh BOOLEAN NOT NULL,
    official_news_healthy BOOLEAN NOT NULL,
    mainstream_news_healthy BOOLEAN NOT NULL,
    tradable BOOLEAN NOT NULL,
    message VARCHAR NOT NULL,
    candidate_count INTEGER NOT NULL,
    metadata_json VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_scores (
    run_id VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    rank INTEGER NOT NULL,
    opportunity_score DOUBLE NOT NULL,
    factor_json VARCHAR NOT NULL,
    risk_event_ids_json VARCHAR NOT NULL,
    blocked BOOLEAN NOT NULL,
    block_reasons_json VARCHAR NOT NULL,
    quote_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, ts_code)
);

CREATE TABLE IF NOT EXISTS paper_account (
    account_id VARCHAR PRIMARY KEY,
    initial_cash DOUBLE NOT NULL,
    cash DOUBLE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    target_value DOUBLE NOT NULL,
    reason VARCHAR NOT NULL,
    position_id VARCHAR,
    UNIQUE(run_id, ts_code, side)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id VARCHAR PRIMARY KEY,
    order_id VARCHAR NOT NULL UNIQUE,
    ts_code VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL,
    shares INTEGER NOT NULL,
    raw_price DOUBLE NOT NULL,
    fill_price DOUBLE NOT NULL,
    commission DOUBLE NOT NULL,
    stamp_duty DOUBLE NOT NULL,
    slippage DOUBLE NOT NULL,
    total_cash_change DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id VARCHAR PRIMARY KEY,
    ts_code VARCHAR NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    opened_trade_date DATE NOT NULL,
    shares INTEGER NOT NULL,
    entry_price DOUBLE NOT NULL,
    cost_basis DOUBLE NOT NULL,
    entry_fees DOUBLE NOT NULL,
    status VARCHAR NOT NULL,
    closed_at TIMESTAMPTZ,
    exit_price DOUBLE,
    exit_reason VARCHAR,
    realized_pnl DOUBLE
);

CREATE TABLE IF NOT EXISTS opportunity_outcomes (
    outcome_id VARCHAR PRIMARY KEY,
    position_id VARCHAR NOT NULL UNIQUE,
    ts_code VARCHAR NOT NULL,
    entry_date DATE NOT NULL,
    first_day_hit BOOLEAN NOT NULL,
    five_day_hit BOOLEAN NOT NULL,
    holding_days INTEGER NOT NULL,
    net_return DOUBLE NOT NULL,
    max_favorable_excursion DOUBLE,
    max_adverse_excursion DOUBLE,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_outcomes (
    outcome_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ts_code VARCHAR NOT NULL,
    signal_kind VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    baseline_at TIMESTAMPTZ,
    baseline_price DOUBLE,
    target_price DOUBLE,
    hit_at TIMESTAMPTZ,
    max_favorable_excursion DOUBLE,
    max_adverse_excursion DOUBLE,
    reason VARCHAR NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(run_id, ts_code)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_runs (
    job_run_id VARCHAR PRIMARY KEY,
    job_name VARCHAR NOT NULL,
    scheduled_for TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    metadata_json VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id VARCHAR PRIMARY KEY,
    report_date DATE NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    report_type VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    model_name VARCHAR NOT NULL,
    UNIQUE(report_date, report_type)
);
"""

MIGRATIONS = (
    Migration(1, "initial_schema", (SCHEMA_SQL,)),
    Migration(
        2,
        "dual_signal_fields_and_historical_st",
        (
            "ALTER TABLE daily_bars ADD COLUMN IF NOT EXISTS is_st BOOLEAN DEFAULT false",
            "ALTER TABLE signal_runs ADD COLUMN IF NOT EXISTS signal_kind VARCHAR DEFAULT 'preclose_entry'",
            "ALTER TABLE signal_runs ADD COLUMN IF NOT EXISTS strategy_version VARCHAR DEFAULT 'preclose-v1'",
            "ALTER TABLE signal_runs ADD COLUMN IF NOT EXISTS information_cutoff TIMESTAMPTZ",
            "ALTER TABLE signal_runs ADD COLUMN IF NOT EXISTS data_as_of TIMESTAMPTZ",
            "UPDATE signal_runs SET signal_kind='preclose_entry' WHERE signal_kind IS NULL",
            "UPDATE signal_runs SET strategy_version='preclose-v1' WHERE strategy_version IS NULL",
            "UPDATE signal_runs SET information_cutoff=as_of WHERE information_cutoff IS NULL",
        ),
    ),
    Migration(
        3,
        "research_run_lifecycle",
        (
            "ALTER TABLE signal_runs ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR DEFAULT 'published'",
            """UPDATE signal_runs SET lifecycle_state = CASE status
               WHEN 'running' THEN 'evaluating'
               WHEN 'failed' THEN 'failed'
               WHEN 'missed' THEN 'missed'
               ELSE 'published' END""",
        ),
    ),
    Migration(
        4,
        "job_leases",
        (
            """CREATE TABLE IF NOT EXISTS job_leases (
               job_run_id VARCHAR PRIMARY KEY,
               heartbeat_at TIMESTAMPTZ NOT NULL,
               lease_expires_at TIMESTAMPTZ NOT NULL,
               recovery_count INTEGER NOT NULL DEFAULT 0
               )""",
            """INSERT INTO job_leases (job_run_id, heartbeat_at, lease_expires_at, recovery_count)
               SELECT job_run_id, started_at, started_at, 0
               FROM job_runs WHERE status='running'
               ON CONFLICT (job_run_id) DO NOTHING""",
        ),
    ),
    Migration(
        5,
        "strategy_attributions",
        (
            """CREATE TABLE IF NOT EXISTS strategy_attributions (
               entity_kind VARCHAR NOT NULL,
               entity_id VARCHAR NOT NULL,
               strategy_id VARCHAR NOT NULL,
               strategy_version VARCHAR NOT NULL,
               parameter_hash VARCHAR NOT NULL,
               parameter_snapshot_json VARCHAR NOT NULL,
               PRIMARY KEY (entity_kind, entity_id)
               )""",
        ),
    ),
)


class JobAlreadyRunningError(RuntimeError):
    """Raised when a live lease already owns the same scheduled job."""


class JobLeaseLostError(RuntimeError):
    """Raised when a worker attempts to write after its lease expired or was recovered."""


class Database:
    def __init__(
        self,
        path: str | Path,
        initial_cash: float = 100_000.0,
        lock_timeout_seconds: int = 30,
        lock_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_cash = initial_cash
        resolved_lock = Path(lock_path) if lock_path else Path(f"{self.path}.lock")
        resolved_lock.parent.mkdir(parents=True, exist_ok=True)
        resolved_lock.touch(exist_ok=True)
        resolved_lock.chmod(0o666)
        self.lock = FileLock(resolved_lock, timeout=lock_timeout_seconds)

    @contextmanager
    def connect(self, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.lock:
            connection = duckdb.connect(str(self.path), read_only=read_only)
            try:
                yield connection
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            MigrationRunner(connection).apply(MIGRATIONS)
            connection.execute("BEGIN TRANSACTION")
            try:
                self._backfill_strategy_attributions(connection)
                connection.execute(
                    "INSERT INTO paper_account(account_id, initial_cash, cash) "
                    "SELECT 'default', ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM paper_account WHERE account_id='default')",
                    [self.initial_cash, self.initial_cash],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _backfill_strategy_attributions(connection: duckdb.DuckDBPyConnection) -> None:
        legacy = [LEGACY_PARAMETER_SNAPSHOT.parameter_hash, LEGACY_PARAMETER_SNAPSHOT.canonical_json]
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT 'signal_run', run_id,
                      CASE signal_kind
                        WHEN 'morning_watchlist' THEN 'morning-watchlist'
                        WHEN 'preclose_entry' THEN 'preclose-entry'
                        ELSE 'legacy-unknown'
                      END,
                      coalesce(strategy_version, 'legacy-v0'), ?, ?
               FROM signal_runs
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions a
                 WHERE a.entity_kind='signal_run' AND a.entity_id=signal_runs.run_id
               )""",
            legacy,
        )
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT 'paper_order', o.order_id, a.strategy_id, a.strategy_version,
                      a.parameter_hash, a.parameter_snapshot_json
               FROM paper_orders o
               JOIN strategy_attributions a
                 ON a.entity_kind='signal_run' AND a.entity_id=o.run_id
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions existing
                 WHERE existing.entity_kind='paper_order' AND existing.entity_id=o.order_id
               )"""
        )
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT DISTINCT 'paper_position', p.position_id, a.strategy_id, a.strategy_version,
                      a.parameter_hash, a.parameter_snapshot_json
               FROM paper_positions p
               JOIN paper_orders o ON o.position_id=p.position_id
               JOIN strategy_attributions a
                 ON a.entity_kind='paper_order' AND a.entity_id=o.order_id
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions existing
                 WHERE existing.entity_kind='paper_position' AND existing.entity_id=p.position_id
               )"""
        )
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT 'paper_order', o.order_id, a.strategy_id, a.strategy_version,
                      a.parameter_hash, a.parameter_snapshot_json
               FROM paper_orders o
               JOIN strategy_attributions a
                 ON a.entity_kind='paper_position' AND a.entity_id=o.position_id
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions existing
                 WHERE existing.entity_kind='paper_order' AND existing.entity_id=o.order_id
               )"""
        )
        for entity_kind, table, id_expression in (
            ("paper_position", "paper_positions", "position_id"),
            ("paper_order", "paper_orders", "order_id"),
        ):
            connection.execute(
                f"""INSERT INTO strategy_attributions
                    SELECT ?, {id_expression}, 'legacy-unknown', 'legacy-v0', ?, ? FROM {table}
                    WHERE NOT EXISTS (
                      SELECT 1 FROM strategy_attributions a
                      WHERE a.entity_kind=? AND a.entity_id={table}.{id_expression}
                    )""",
                [entity_kind, *legacy, entity_kind],
            )
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT 'candidate_outcome', c.run_id || ':' || c.ts_code,
                      a.strategy_id, a.strategy_version, a.parameter_hash, a.parameter_snapshot_json
               FROM candidate_outcomes c
               JOIN strategy_attributions a
                 ON a.entity_kind='signal_run' AND a.entity_id=c.run_id
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions existing
                 WHERE existing.entity_kind='candidate_outcome'
                   AND existing.entity_id=c.run_id || ':' || c.ts_code
               )"""
        )
        connection.execute(
            """INSERT INTO strategy_attributions
               SELECT 'opportunity_outcome', o.position_id,
                      a.strategy_id, a.strategy_version, a.parameter_hash, a.parameter_snapshot_json
               FROM opportunity_outcomes o
               JOIN strategy_attributions a
                 ON a.entity_kind='paper_position' AND a.entity_id=o.position_id
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_attributions existing
                 WHERE existing.entity_kind='opportunity_outcome' AND existing.entity_id=o.position_id
               )"""
        )

    @staticmethod
    def _register_upsert(
        connection: duckdb.DuckDBPyConnection,
        table: str,
        frame: pd.DataFrame,
        columns: list[str],
    ) -> None:
        if frame.empty:
            return
        view_name = f"incoming_{table}"
        connection.register(view_name, frame[columns])
        names = ", ".join(columns)
        connection.execute(f"INSERT OR REPLACE INTO {table} ({names}) SELECT {names} FROM {view_name}")
        connection.unregister(view_name)

    def upsert_securities(self, frame: pd.DataFrame) -> None:
        columns = ["ts_code", "symbol", "name", "exchange", "market", "list_date", "delist_date", "list_status"]
        with self.connect() as connection:
            self._register_upsert(connection, "securities", frame, columns)

    def upsert_trade_calendar(self, frame: pd.DataFrame) -> None:
        columns = ["cal_date", "is_open", "pretrade_date"]
        with self.connect() as connection:
            self._register_upsert(connection, "trade_calendar", frame, columns)

    def upsert_daily_bars(self, frame: pd.DataFrame) -> None:
        frame = frame.copy()
        if "is_st" not in frame.columns:
            frame["is_st"] = False
        columns = [
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "amount",
            "adj_factor",
            "up_limit",
            "down_limit",
            "suspended",
            "is_st",
        ]
        with self.connect() as connection:
            self._register_upsert(connection, "daily_bars", frame, columns)

    def insert_live_quotes(self, frame: pd.DataFrame) -> None:
        columns = ["ts_code", "captured_at", "price", "open", "high", "low", "pre_close", "volume", "amount", "source"]
        with self.connect() as connection:
            self._register_upsert(connection, "live_quotes", frame, columns)

    def get_securities(self) -> pd.DataFrame:
        with self.connect(read_only=True) as connection:
            return connection.execute("SELECT * EXCLUDE(updated_at) FROM securities").fetchdf()

    def get_recent_daily_bars(self, trading_days: int = 30, as_of: date | None = None) -> pd.DataFrame:
        where = ""
        params: list[Any] = []
        if as_of is not None:
            where = "WHERE trade_date <= ?"
            params.append(as_of)
        query = f"""
            WITH recent_dates AS (
                SELECT DISTINCT trade_date FROM daily_bars {where}
                ORDER BY trade_date DESC LIMIT ?
            )
            SELECT * FROM daily_bars
            WHERE trade_date IN (SELECT trade_date FROM recent_dates)
            ORDER BY ts_code, trade_date
        """
        params.append(trading_days)
        with self.connect(read_only=True) as connection:
            return connection.execute(query, params).fetchdf()

    def get_latest_quotes(self, at_or_before: datetime | None = None) -> pd.DataFrame:
        time_filter = ""
        params: list[Any] = []
        if at_or_before is not None:
            time_filter = "WHERE captured_at <= ?"
            params.append(at_or_before)
        query = f"""
            SELECT * EXCLUDE(row_number) FROM (
                SELECT *, row_number() OVER(PARTITION BY ts_code ORDER BY captured_at DESC) AS row_number
                FROM live_quotes {time_filter}
            ) WHERE row_number=1
        """
        with self.connect(read_only=True) as connection:
            return connection.execute(query, params).fetchdf()

    def get_quote_near(self, ts_code: str, at_or_before: datetime) -> dict[str, Any] | None:
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM live_quotes WHERE ts_code=? AND captured_at<=? ORDER BY captured_at DESC LIMIT 1",
                [ts_code, at_or_before],
            ).fetchone()
            if row is None:
                return None
            columns = [item[0] for item in connection.description]
            return dict(zip(columns, row, strict=True))

    def save_news_documents(self, documents: list[NewsDocument]) -> int:
        inserted = 0
        with self.connect() as connection:
            for document in documents:
                before = connection.execute(
                    "SELECT count(*) FROM news_documents WHERE content_hash=?", [document.content_hash]
                ).fetchone()[0]
                if before:
                    continue
                connection.execute(
                    """INSERT INTO news_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        document.document_id,
                        document.ts_code,
                        document.title,
                        document.content,
                        document.published_at,
                        document.source,
                        document.source_tier.value,
                        document.url,
                        document.content_hash,
                        document.fetched_at,
                        document.processing_status,
                        document.processing_error,
                    ],
                )
                inserted += 1
        return inserted

    def pending_news_documents(self, limit: int = 500) -> list[NewsDocument]:
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                "SELECT * FROM news_documents WHERE processing_status='pending' ORDER BY published_at LIMIT ?",
                [limit],
            ).fetchdf()
        return [NewsDocument.model_validate(row) for row in frame.to_dict("records")]

    def mark_document(
        self, document_id: str, status: str, error: str | None = None, content: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE news_documents SET processing_status=?, processing_error=?,
                   content=coalesce(?, content) WHERE document_id=?""",
                [status, error, content, document_id],
            )

    def save_risk_events(self, events: list[RiskEvent]) -> None:
        with self.connect() as connection:
            for event in events:
                connection.execute(
                    "INSERT OR REPLACE INTO risk_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        event.event_id,
                        event.document_id,
                        event.ts_code,
                        event.event_type,
                        event.direction.value,
                        event.severity.value,
                        event.confidence,
                        event.hard_block,
                        event.evidence,
                        event.source_url,
                        event.published_at,
                        event.extracted_at,
                        event.model_name,
                    ],
                )

    def get_risk_events(self, start: datetime, end: datetime) -> pd.DataFrame:
        with self.connect(read_only=True) as connection:
            return connection.execute(
                "SELECT * FROM risk_events WHERE published_at BETWEEN ? AND ? ORDER BY published_at DESC",
                [start, end],
            ).fetchdf()

    def unprocessed_official_codes(self, start: datetime, as_of: datetime) -> set[str]:
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT DISTINCT ts_code FROM news_documents
                   WHERE source_tier='official' AND published_at BETWEEN ? AND ?
                     AND processing_status<>'processed' AND ts_code IS NOT NULL""",
                [start, as_of],
            ).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def _write_strategy_attribution(
        connection: duckdb.DuckDBPyConnection,
        entity_kind: str,
        entity_id: str,
        attribution: StrategyAttribution,
    ) -> None:
        values = (
            attribution.strategy_id,
            attribution.strategy_version,
            attribution.parameter_hash,
            canonical_parameter_json(attribution.strategy_parameters),
        )
        if parameter_hash(values[3]) != attribution.parameter_hash:
            raise ValueError("parameter_hash does not match the current strategy parameter snapshot")
        existing = connection.execute(
            """SELECT strategy_id, strategy_version, parameter_hash, parameter_snapshot_json
               FROM strategy_attributions WHERE entity_kind=? AND entity_id=?""",
            [entity_kind, entity_id],
        ).fetchone()
        if existing is not None:
            if existing != values:
                raise ValueError(f"strategy attribution is immutable for {entity_kind}:{entity_id}")
            return
        connection.execute(
            """INSERT INTO strategy_attributions VALUES (?, ?, ?, ?, ?, ?)""",
            [entity_kind, entity_id, *values],
        )

    @classmethod
    def _write_signal_run(cls, connection: duckdb.DuckDBPyConnection, run: SignalRun) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO signal_runs (
               run_id, as_of, signal_kind, strategy_version, information_cutoff, data_as_of,
               status, lifecycle_state, data_fresh, official_news_healthy, mainstream_news_healthy,
               tradable, message, candidate_count, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                run.run_id,
                run.as_of,
                run.signal_kind.value,
                run.strategy_version,
                run.information_cutoff or run.as_of,
                run.data_as_of,
                run.status.value,
                run.lifecycle_state.value,
                run.data_fresh,
                run.official_news_healthy,
                run.mainstream_news_healthy,
                run.tradable,
                run.message,
                run.candidate_count,
                json.dumps(run.metadata, ensure_ascii=False, sort_keys=True),
            ],
        )
        cls._write_strategy_attribution(connection, "signal_run", run.run_id, run)

    @staticmethod
    def _write_candidates(
        connection: duckdb.DuckDBPyConnection,
        candidates: list[CandidateScore],
    ) -> None:
        for candidate in candidates:
            connection.execute(
                "INSERT OR REPLACE INTO candidate_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    candidate.run_id,
                    candidate.ts_code,
                    candidate.name,
                    candidate.rank,
                    candidate.opportunity_score,
                    candidate.factor_breakdown.model_dump_json(),
                    json.dumps(candidate.risk_event_ids, ensure_ascii=False),
                    candidate.blocked,
                    json.dumps(candidate.block_reasons, ensure_ascii=False),
                    candidate.quote_at,
                ],
            )

    @classmethod
    def _write_order(cls, connection: duckdb.DuckDBPyConnection, order: PaperOrder) -> bool:
        exists = connection.execute(
            "SELECT 1 FROM paper_orders WHERE run_id=? AND ts_code=? AND side=?",
            [order.run_id, order.ts_code, order.side.value],
        ).fetchone()
        if exists:
            return False
        connection.execute(
            "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                order.order_id,
                order.run_id,
                order.ts_code,
                order.side.value,
                order.status.value,
                order.created_at,
                order.target_value,
                order.reason,
                order.position_id,
            ],
        )
        cls._write_strategy_attribution(connection, "paper_order", order.order_id, order)
        return True

    @staticmethod
    def _write_job(
        connection: duckdb.DuckDBPyConnection,
        job_run_id: str,
        job_name: str,
        started_at: datetime,
        status: str,
        message: str,
        finished_at: datetime | None = None,
        scheduled_for: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO job_runs (
               job_run_id, job_name, scheduled_for, started_at, finished_at, status, message, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (job_run_id) DO UPDATE SET
                 job_name=excluded.job_name,
                 scheduled_for=excluded.scheduled_for,
                 started_at=excluded.started_at,
                 finished_at=excluded.finished_at,
                 status=excluded.status,
                 message=excluded.message,
                 metadata_json=excluded.metadata_json""",
            [
                job_run_id,
                job_name,
                scheduled_for,
                started_at,
                finished_at,
                status,
                message,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ],
        )

    @staticmethod
    def _ensure_legacy_job_leases(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            """INSERT INTO job_leases (job_run_id, heartbeat_at, lease_expires_at, recovery_count)
               SELECT job_run_id, started_at, started_at, 0
               FROM job_runs
               WHERE status='running'
                 AND NOT EXISTS (
                   SELECT 1 FROM job_leases WHERE job_leases.job_run_id=job_runs.job_run_id
                 )
               ON CONFLICT (job_run_id) DO NOTHING"""
        )

    @classmethod
    def _recover_expired_jobs(
        cls, connection: duckdb.DuckDBPyConnection, recovered_at: datetime
    ) -> list[str]:
        cls._ensure_legacy_job_leases(connection)
        expired = [
            str(row[0])
            for row in connection.execute(
                """SELECT job_runs.job_run_id
                   FROM job_runs JOIN job_leases USING (job_run_id)
                   WHERE job_runs.status='running' AND job_leases.lease_expires_at < ?
                   ORDER BY job_runs.started_at, job_runs.job_run_id""",
                [recovered_at],
            ).fetchall()
        ]
        for job_run_id in expired:
            connection.execute(
                """UPDATE job_runs
                   SET finished_at=?, status='failed',
                       message='lease expired; recovered after worker interruption'
                   WHERE job_run_id=? AND status='running'""",
                [recovered_at, job_run_id],
            )
            connection.execute(
                "UPDATE job_leases SET recovery_count=recovery_count+1 WHERE job_run_id=?",
                [job_run_id],
            )
        return expired

    @staticmethod
    def _assert_active_job_lease(
        connection: duckdb.DuckDBPyConnection, job_run_id: str, at: datetime
    ) -> None:
        active = connection.execute(
            """SELECT 1 FROM job_runs JOIN job_leases USING (job_run_id)
               WHERE job_run_id=? AND status='running' AND lease_expires_at >= ?""",
            [job_run_id, at],
        ).fetchone()
        if active is None:
            raise JobLeaseLostError(f"job lease is no longer active: {job_run_id}")

    @staticmethod
    def _complete_job_lease(
        connection: duckdb.DuckDBPyConnection, job_run_id: str, finished_at: datetime
    ) -> None:
        connection.execute(
            "UPDATE job_leases SET heartbeat_at=? WHERE job_run_id=?",
            [finished_at, job_run_id],
        )

    def save_signal_run(self, run: SignalRun) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                self._write_signal_run(connection, run)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_candidates(self, candidates: list[CandidateScore]) -> None:
        with self.connect() as connection:
            self._write_candidates(connection, candidates)

    def latest_signal_run(
        self,
        on_date: date | None = None,
        signal_kind: str | None = None,
        include_non_terminal: bool = False,
    ) -> dict[str, Any] | None:
        conditions: list[str] = []
        params: list[Any] = []
        if on_date:
            conditions.append("CAST(r.as_of AS DATE)=?")
            params.append(on_date)
        if signal_kind:
            conditions.append("r.signal_kind=?")
            params.append(signal_kind)
        if not include_non_terminal:
            readable = sorted(state.value for state in READABLE_RESEARCH_RUN_STATES)
            conditions.append(f"r.lifecycle_state IN ({', '.join('?' for _ in readable)})")
            params.extend(readable)
        condition = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                f"""SELECT r.*,
                           coalesce(a.strategy_id, CASE r.signal_kind
                             WHEN 'morning_watchlist' THEN 'morning-watchlist'
                             WHEN 'preclose_entry' THEN 'preclose-entry'
                             ELSE 'legacy-unknown' END) AS strategy_id,
                           coalesce(a.parameter_hash, '{LEGACY_PARAMETER_SNAPSHOT.parameter_hash}') AS parameter_hash,
                           coalesce(a.parameter_snapshot_json,
                                    '{LEGACY_PARAMETER_SNAPSHOT.canonical_json}') AS parameter_snapshot_json
                    FROM signal_runs r
                    LEFT JOIN strategy_attributions a
                      ON a.entity_kind='signal_run' AND a.entity_id=r.run_id
                    {condition} ORDER BY r.as_of DESC LIMIT 1""",
                params,
            ).fetchone()
            if row is None:
                return None
            result = dict(zip([c[0] for c in connection.description], row, strict=True))
            result["strategy_parameters"] = json.loads(result.pop("parameter_snapshot_json"))
            return result

    def get_candidates(self, run_id: str, include_blocked: bool = True) -> pd.DataFrame:
        blocked_filter = "" if include_blocked else "AND NOT blocked"
        readable = sorted(state.value for state in READABLE_RESEARCH_RUN_STATES)
        with self.connect(read_only=True) as connection:
            return connection.execute(
                f"""SELECT candidate_scores.* FROM candidate_scores
                    JOIN signal_runs USING (run_id)
                    WHERE run_id=? {blocked_filter}
                      AND lifecycle_state IN ({', '.join('?' for _ in readable)})
                    ORDER BY rank""",
                [run_id, *readable],
            ).fetchdf()

    def recent_signal_runs(self, limit: int = 100, include_non_terminal: bool = False) -> pd.DataFrame:
        params: list[Any] = []
        where = ""
        if not include_non_terminal:
            readable = sorted(state.value for state in READABLE_RESEARCH_RUN_STATES)
            where = f"WHERE r.lifecycle_state IN ({', '.join('?' for _ in readable)})"
            params.extend(readable)
        params.append(limit)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""SELECT r.*,
                           coalesce(a.strategy_id, CASE r.signal_kind
                             WHEN 'morning_watchlist' THEN 'morning-watchlist'
                             WHEN 'preclose_entry' THEN 'preclose-entry'
                             ELSE 'legacy-unknown' END) AS strategy_id,
                           coalesce(a.parameter_hash, '{LEGACY_PARAMETER_SNAPSHOT.parameter_hash}') AS parameter_hash,
                           coalesce(a.parameter_snapshot_json,
                                    '{LEGACY_PARAMETER_SNAPSHOT.canonical_json}') AS parameter_snapshot_json
                    FROM signal_runs r
                    LEFT JOIN strategy_attributions a
                      ON a.entity_kind='signal_run' AND a.entity_id=r.run_id
                    {where} ORDER BY r.as_of DESC LIMIT ?""",
                params,
            ).fetchdf()
        return self._decode_strategy_frame(frame)

    def get_cash(self) -> float:
        with self.connect(read_only=True) as connection:
            return float(connection.execute("SELECT cash FROM paper_account WHERE account_id='default'").fetchone()[0])

    def get_open_positions(self) -> pd.DataFrame:
        return self.table_with_strategy("paper_positions").query("status == 'open'").sort_values("opened_at")

    def save_order(self, order: PaperOrder) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                created = self._write_order(connection, order)
                connection.execute("COMMIT")
                return created
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def proposed_orders(self, run_id: str | None = None) -> pd.DataFrame:
        condition = "status='proposed'"
        params: list[Any] = []
        if run_id:
            condition += " AND run_id=?"
            params.append(run_id)
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""SELECT e.*, a.strategy_id, a.strategy_version, a.parameter_hash,
                           a.parameter_snapshot_json
                    FROM paper_orders e JOIN strategy_attributions a
                      ON a.entity_kind='paper_order' AND a.entity_id=e.order_id
                    WHERE {condition} ORDER BY created_at""",
                params,
            ).fetchdf()
        return self._decode_strategy_frame(frame)

    def reject_order(self, order_id: str, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE paper_orders SET status='rejected', reason=? WHERE order_id=? AND status='proposed'",
                [reason, order_id],
            )

    def apply_buy_fill(self, fill: PaperFill, position: PaperPosition) -> None:
        cash_required = -fill.total_cash_change
        with self.connect() as connection:
            connection.begin()
            try:
                cash = float(
                    connection.execute("SELECT cash FROM paper_account WHERE account_id='default'").fetchone()[0]
                )
                if cash + 1e-9 < cash_required:
                    raise ValueError("insufficient paper cash")
                if connection.execute("SELECT 1 FROM paper_fills WHERE order_id=?", [fill.order_id]).fetchone():
                    connection.rollback()
                    return
                connection.execute(
                    "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        fill.fill_id,
                        fill.order_id,
                        fill.ts_code,
                        fill.side.value,
                        fill.filled_at,
                        fill.shares,
                        fill.raw_price,
                        fill.fill_price,
                        fill.commission,
                        fill.stamp_duty,
                        fill.slippage,
                        fill.total_cash_change,
                    ],
                )
                connection.execute(
                    "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        position.position_id,
                        position.ts_code,
                        position.opened_at,
                        position.opened_trade_date,
                        position.shares,
                        position.entry_price,
                        position.cost_basis,
                        position.entry_fees,
                        position.status,
                        position.closed_at,
                        position.exit_price,
                        position.exit_reason,
                        position.realized_pnl,
                    ],
                )
                self._write_strategy_attribution(
                    connection,
                    "paper_position",
                    position.position_id,
                    position,
                )
                connection.execute(
                    "UPDATE paper_account SET cash=cash-?, updated_at=now() WHERE account_id='default'",
                    [cash_required],
                )
                connection.execute(
                    "UPDATE paper_orders SET status='filled', position_id=? WHERE order_id=?",
                    [position.position_id, fill.order_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_sell_order_if_absent(self, order: PaperOrder) -> bool:
        return self.save_order(order)

    def apply_sell_fill(self, fill: PaperFill, position_id: str, reason: str) -> PaperPosition:
        with self.connect() as connection:
            connection.begin()
            try:
                row = connection.execute(
                    """SELECT p.*, a.strategy_id, a.strategy_version, a.parameter_hash,
                              a.parameter_snapshot_json
                       FROM paper_positions p JOIN strategy_attributions a
                         ON a.entity_kind='paper_position' AND a.entity_id=p.position_id
                       WHERE p.position_id=? AND p.status='open'""",
                    [position_id],
                ).fetchone()
                if row is None:
                    raise ValueError("open position not found")
                columns = [c[0] for c in connection.description]
                current = dict(zip(columns, row, strict=True))
                current["strategy_parameters"] = json.loads(current.pop("parameter_snapshot_json"))
                if connection.execute("SELECT 1 FROM paper_fills WHERE order_id=?", [fill.order_id]).fetchone():
                    connection.rollback()
                    return PaperPosition.model_validate(current)
                connection.execute(
                    "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        fill.fill_id,
                        fill.order_id,
                        fill.ts_code,
                        fill.side.value,
                        fill.filled_at,
                        fill.shares,
                        fill.raw_price,
                        fill.fill_price,
                        fill.commission,
                        fill.stamp_duty,
                        fill.slippage,
                        fill.total_cash_change,
                    ],
                )
                proceeds = fill.total_cash_change
                invested = current["cost_basis"] * current["shares"]
                realized = proceeds - invested
                connection.execute(
                    """UPDATE paper_positions SET status='closed', closed_at=?, exit_price=?,
                       exit_reason=?, realized_pnl=? WHERE position_id=?""",
                    [fill.filled_at, fill.fill_price, reason, realized, position_id],
                )
                connection.execute(
                    "UPDATE paper_account SET cash=cash+?, updated_at=now() WHERE account_id='default'",
                    [proceeds],
                )
                connection.execute("UPDATE paper_orders SET status='filled' WHERE order_id=?", [fill.order_id])
                connection.commit()
                current.update(
                    status="closed",
                    closed_at=fill.filled_at,
                    exit_price=fill.fill_price,
                    exit_reason=reason,
                    realized_pnl=realized,
                )
                return PaperPosition.model_validate(current)
            except Exception:
                connection.rollback()
                raise

    def save_outcome(self, outcome: OpportunityOutcome) -> None:
        with self.connect() as connection:
            connection.begin()
            try:
                connection.execute(
                    """INSERT INTO opportunity_outcomes (
                   outcome_id, position_id, ts_code, entry_date, first_day_hit, five_day_hit,
                   holding_days, net_return, max_favorable_excursion, max_adverse_excursion, recorded_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (position_id) DO UPDATE SET
                     outcome_id=excluded.outcome_id,
                     ts_code=excluded.ts_code,
                     entry_date=excluded.entry_date,
                     first_day_hit=excluded.first_day_hit,
                     five_day_hit=excluded.five_day_hit,
                     holding_days=excluded.holding_days,
                     net_return=excluded.net_return,
                     max_favorable_excursion=excluded.max_favorable_excursion,
                     max_adverse_excursion=excluded.max_adverse_excursion,
                   recorded_at=excluded.recorded_at""",
                    [
                        outcome.outcome_id,
                        outcome.position_id,
                        outcome.ts_code,
                        outcome.entry_date,
                        outcome.first_day_hit,
                        outcome.five_day_hit,
                        outcome.holding_days,
                        outcome.net_return,
                        outcome.max_favorable_excursion,
                        outcome.max_adverse_excursion,
                        outcome.recorded_at,
                    ],
                )
                self._write_strategy_attribution(
                    connection,
                    "opportunity_outcome",
                    outcome.position_id,
                    outcome,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_candidate_outcome(self, outcome: CandidateOutcome) -> None:
        with self.connect() as connection:
            connection.begin()
            try:
                connection.execute(
                    """INSERT INTO candidate_outcomes (
                   outcome_id, run_id, ts_code, signal_kind, status, baseline_at,
                   baseline_price, target_price, hit_at, max_favorable_excursion,
                   max_adverse_excursion, reason, evaluated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (run_id, ts_code) DO UPDATE SET
                     outcome_id=excluded.outcome_id,
                     signal_kind=excluded.signal_kind,
                     status=excluded.status,
                     baseline_at=excluded.baseline_at,
                     baseline_price=excluded.baseline_price,
                     target_price=excluded.target_price,
                     hit_at=excluded.hit_at,
                     max_favorable_excursion=excluded.max_favorable_excursion,
                     max_adverse_excursion=excluded.max_adverse_excursion,
                     reason=excluded.reason,
                   evaluated_at=excluded.evaluated_at""",
                    [
                        outcome.outcome_id,
                        outcome.run_id,
                        outcome.ts_code,
                        outcome.signal_kind.value,
                        outcome.status.value,
                        outcome.baseline_at,
                        outcome.baseline_price,
                        outcome.target_price,
                        outcome.hit_at,
                        outcome.max_favorable_excursion,
                        outcome.max_adverse_excursion,
                        outcome.reason,
                        outcome.evaluated_at,
                    ],
                )
                self._write_strategy_attribution(
                    connection,
                    "candidate_outcome",
                    f"{outcome.run_id}:{outcome.ts_code}",
                    outcome,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def candidate_outcomes(self, signal_kind: str | None = None) -> pd.DataFrame:
        where = "WHERE e.signal_kind=?" if signal_kind else ""
        params = [signal_kind] if signal_kind else []
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""SELECT e.*, a.strategy_id, a.strategy_version, a.parameter_hash,
                           a.parameter_snapshot_json
                    FROM candidate_outcomes e JOIN strategy_attributions a
                      ON a.entity_kind='candidate_outcome'
                     AND a.entity_id=e.run_id || ':' || e.ts_code
                    {where} ORDER BY e.evaluated_at DESC""",
                params,
            ).fetchdf()
        return self._decode_strategy_frame(frame)

    def latest_job(self, job_name: str | None = None) -> dict[str, Any] | None:
        where = "WHERE job_name=?" if job_name else ""
        params = [job_name] if job_name else []
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT * FROM job_runs {where} ORDER BY started_at DESC LIMIT 1", params
            ).fetchone()
            if row is None:
                return None
            return dict(zip([item[0] for item in connection.description], row, strict=True))

    def migration_version(self) -> int:
        with self.connect(read_only=True) as connection:
            return int(connection.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0])

    def count_trading_days(self, start: date, end: date) -> int:
        with self.connect(read_only=True) as connection:
            result = connection.execute(
                "SELECT count(*) FROM trade_calendar WHERE is_open AND cal_date BETWEEN ? AND ?",
                [start, end],
            ).fetchone()[0]
            if result:
                return int(result)
            return max((end - start).days + 1, 1)

    def is_trading_day(self, value: date) -> bool:
        with self.connect(read_only=True) as connection:
            row = connection.execute("SELECT is_open FROM trade_calendar WHERE cal_date=?", [value]).fetchone()
        return bool(row and row[0])

    def previous_trading_day(self, value: date) -> date | None:
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT pretrade_date FROM trade_calendar WHERE cal_date=? AND is_open",
                [value],
            ).fetchone()
        return row[0] if row and row[0] else None

    def trading_day_lookback(self, value: date, trading_days: int) -> date | None:
        if trading_days < 1:
            raise ValueError("trading_days must be positive")
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                """SELECT cal_date FROM trade_calendar
                   WHERE is_open AND cal_date<=? ORDER BY cal_date DESC LIMIT 1 OFFSET ?""",
                [value, trading_days - 1],
            ).fetchone()
        return row[0] if row else None

    def start_job(
        self,
        job_run_id: str,
        job_name: str,
        started_at: datetime,
        lease_duration: timedelta,
        scheduled_for: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        if lease_duration <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                recovered = self._recover_expired_jobs(connection, started_at)
                active = connection.execute(
                    """SELECT job_runs.job_run_id
                       FROM job_runs LEFT JOIN job_leases USING (job_run_id)
                       WHERE job_name=? AND status='running'
                         AND (job_leases.job_run_id IS NULL OR lease_expires_at >= ?)
                       LIMIT 1""",
                    [job_name, started_at],
                ).fetchone()
                if active is not None:
                    raise JobAlreadyRunningError(f"job already has an active lease: {job_name}")
                self._write_job(
                    connection,
                    job_run_id,
                    job_name,
                    started_at,
                    "running",
                    "started",
                    scheduled_for=scheduled_for,
                    metadata=metadata,
                )
                connection.execute(
                    """INSERT INTO job_leases (
                       job_run_id, heartbeat_at, lease_expires_at, recovery_count
                       ) VALUES (?, ?, ?, 0)
                       ON CONFLICT (job_run_id) DO UPDATE SET
                         heartbeat_at=excluded.heartbeat_at,
                         lease_expires_at=excluded.lease_expires_at,
                         recovery_count=0""",
                    [job_run_id, started_at, started_at + lease_duration],
                )
                connection.execute("COMMIT")
                if recovered:
                    LOGGER.warning("recovered expired jobs before starting %s: %s", job_name, ",".join(recovered))
                return recovered
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def heartbeat_job(self, job_run_id: str, heartbeat_at: datetime, lease_duration: timedelta) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        with self.connect() as connection:
            renewed = connection.execute(
                """UPDATE job_leases
                   SET heartbeat_at=?, lease_expires_at=?
                   WHERE job_run_id=? AND lease_expires_at >= ?
                     AND EXISTS (
                       SELECT 1 FROM job_runs
                       WHERE job_runs.job_run_id=job_leases.job_run_id AND status='running'
                     )
                   RETURNING job_run_id""",
                [heartbeat_at, heartbeat_at + lease_duration, job_run_id, heartbeat_at],
            ).fetchone()
        return renewed is not None

    def finish_job(
        self,
        job_run_id: str,
        finished_at: datetime,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"finish_job requires a terminal status: {status}")
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                self._assert_active_job_lease(connection, job_run_id, finished_at)
                current = connection.execute(
                    "SELECT job_name, started_at, scheduled_for FROM job_runs WHERE job_run_id=?",
                    [job_run_id],
                ).fetchone()
                self._write_job(
                    connection,
                    job_run_id,
                    str(current[0]),
                    current[1],
                    status,
                    message,
                    finished_at=finished_at,
                    scheduled_for=current[2],
                    metadata=metadata,
                )
                self._complete_job_lease(connection, job_run_id, finished_at)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def recover_expired_jobs(self, recovered_at: datetime) -> list[str]:
        with self.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                recovered = self._recover_expired_jobs(connection, recovered_at)
                connection.execute("COMMIT")
                if recovered:
                    LOGGER.warning("recovered expired jobs: %s", ",".join(recovered))
                return recovered
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_report(
        self, report_id: str, report_date: date, generated_at: datetime, report_type: str, content: str, model_name: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO reports (
                   report_id, report_date, generated_at, report_type, content, model_name
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (report_date, report_type) DO UPDATE SET
                     report_id=excluded.report_id,
                     generated_at=excluded.generated_at,
                     content=excluded.content,
                     model_name=excluded.model_name""",
                [report_id, report_date, generated_at, report_type, content, model_name],
            )

    def table(self, name: str, limit: int = 1000) -> pd.DataFrame:
        allowed = {
            "signal_runs",
            "candidate_scores",
            "paper_account",
            "paper_orders",
            "paper_fills",
            "paper_positions",
            "opportunity_outcomes",
            "candidate_outcomes",
            "risk_events",
            "news_documents",
            "job_runs",
            "job_leases",
            "reports",
            "strategy_attributions",
        }
        if name not in allowed:
            raise ValueError(f"table not allowed: {name}")
        with self.connect(read_only=True) as connection:
            return connection.execute(f"SELECT * FROM {name} ORDER BY 1 DESC LIMIT ?", [limit]).fetchdf()

    @staticmethod
    def _decode_strategy_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if "parameter_snapshot_json" not in frame.columns:
            return frame
        decoded = frame.copy()
        decoded["strategy_parameters"] = decoded["parameter_snapshot_json"].map(json.loads)
        return decoded.drop(columns=["parameter_snapshot_json"])

    def table_with_strategy(self, name: str, limit: int = 1000) -> pd.DataFrame:
        attributed = {
            "signal_runs": ("signal_run", "e.run_id"),
            "paper_orders": ("paper_order", "e.order_id"),
            "paper_positions": ("paper_position", "e.position_id"),
            "candidate_outcomes": ("candidate_outcome", "e.run_id || ':' || e.ts_code"),
            "opportunity_outcomes": ("opportunity_outcome", "e.position_id"),
        }
        if name not in attributed:
            raise ValueError(f"strategy attribution not supported for table: {name}")
        entity_kind, entity_id = attributed[name]
        strategy_version = "" if name == "signal_runs" else ", a.strategy_version"
        with self.connect(read_only=True) as connection:
            frame = connection.execute(
                f"""SELECT e.*, a.strategy_id{strategy_version}, a.parameter_hash,
                           a.parameter_snapshot_json
                    FROM {name} e JOIN strategy_attributions a
                      ON a.entity_kind=? AND a.entity_id={entity_id}
                    ORDER BY 1 DESC LIMIT ?""",
                [entity_kind, limit],
            ).fetchdf()
        return self._decode_strategy_frame(frame)
