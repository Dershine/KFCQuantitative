from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import duckdb

from kfcquant.db import Database
from kfcquant.models import (
    CandidateScore,
    OrderSide,
    PaperOrder,
    ResearchRunState,
    RunStatus,
    SignalRun,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobCompletion:
    job_run_id: str
    job_name: str
    started_at: datetime
    finished_at: datetime
    status: str
    message: str
    scheduled_for: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ResearchRunUnitOfWork(Protocol):
    def commit(
        self,
        run: SignalRun,
        candidates: list[CandidateScore],
        orders: list[PaperOrder],
        job: JobCompletion,
    ) -> bool: ...


class DuckDBResearchRunUnitOfWork:
    """Atomically expose one terminal Research Run and all of its publication rows."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validate(
        run: SignalRun,
        candidates: list[CandidateScore],
        orders: list[PaperOrder],
        job: JobCompletion,
    ) -> None:
        if run.lifecycle_state not in {
            ResearchRunState.PUBLISHED,
            ResearchRunState.FAILED,
            ResearchRunState.MISSED,
        }:
            raise ValueError("unit of work requires a published, failed, or missed research run")
        if any(candidate.run_id != run.run_id for candidate in candidates):
            raise ValueError("every candidate must belong to the published run")
        if any(order.run_id != run.run_id for order in orders):
            raise ValueError("every order must belong to the published run")
        if job.status != run.status.value:
            raise ValueError("job completion status must match the research run result")
        if run.lifecycle_state != ResearchRunState.PUBLISHED:
            if candidates or orders or run.tradable:
                raise ValueError("failed or missed runs cannot publish candidates or orders")
            return
        if run.status not in {RunStatus.SUCCESS, RunStatus.DEGRADED}:
            raise ValueError("a published run requires a successful or degraded result")
        unblocked = {candidate.ts_code for candidate in candidates if not candidate.blocked}
        if len({(candidate.run_id, candidate.ts_code) for candidate in candidates}) != len(candidates):
            raise ValueError("publication contains duplicate candidates")
        if run.candidate_count != len(unblocked):
            raise ValueError("candidate_count must match unique unblocked candidates")
        if not run.tradable and orders:
            raise ValueError("non-tradable runs cannot create orders")
        if any(order.side != OrderSide.BUY for order in orders):
            raise ValueError("research run publication can only create buy orders")
        if any(order.ts_code not in unblocked for order in orders):
            raise ValueError("blocked or missing candidates cannot create orders")
        order_keys = {(order.run_id, order.ts_code, order.side) for order in orders}
        order_ids = {order.order_id for order in orders}
        if len(order_keys) != len(orders) or len(order_ids) != len(orders):
            raise ValueError("publication contains duplicate orders")

    @staticmethod
    def _is_identical_existing_publication(
        connection: duckdb.DuckDBPyConnection,
        run: SignalRun,
        candidates: list[CandidateScore],
        orders: list[PaperOrder],
        job: JobCompletion,
    ) -> bool:
        stored = connection.execute(
            """SELECT status, lifecycle_state, tradable, candidate_count
               FROM signal_runs WHERE run_id=?""",
            [run.run_id],
        ).fetchone()
        if stored is None:
            return False
        expected_run = (run.status.value, run.lifecycle_state.value, run.tradable, run.candidate_count)
        stored_candidates = connection.execute(
            "SELECT ts_code, rank, blocked, opportunity_score FROM candidate_scores WHERE run_id=? ORDER BY ts_code",
            [run.run_id],
        ).fetchall()
        expected_candidates = sorted(
            (candidate.ts_code, candidate.rank, candidate.blocked, candidate.opportunity_score)
            for candidate in candidates
        )
        stored_orders = connection.execute(
            "SELECT order_id, ts_code, side, status, target_value FROM paper_orders WHERE run_id=? ORDER BY order_id",
            [run.run_id],
        ).fetchall()
        expected_orders = sorted(
            (order.order_id, order.ts_code, order.side.value, order.status.value, order.target_value)
            for order in orders
        )
        stored_job = connection.execute(
            "SELECT job_name, status, message FROM job_runs WHERE job_run_id=?",
            [job.job_run_id],
        ).fetchone()
        expected_job = (job.job_name, job.status, job.message)
        if (
            stored == expected_run
            and stored_candidates == expected_candidates
            and stored_orders == expected_orders
            and stored_job == expected_job
        ):
            return True
        raise ValueError("published research run is immutable and conflicts with existing rows")

    def _checkpoint(self, stage: str) -> None:
        """Fault-injection seam used by component tests."""

    def commit(
        self,
        run: SignalRun,
        candidates: list[CandidateScore],
        orders: list[PaperOrder],
        job: JobCompletion,
    ) -> bool:
        self._validate(run, candidates, orders, job)
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                if self._is_identical_existing_publication(connection, run, candidates, orders, job):
                    connection.execute("COMMIT")
                    return False
                self.database._assert_active_job_lease(connection, job.job_run_id, job.finished_at)
                self.database._write_signal_run(connection, run)
                self._checkpoint("run")
                self.database._write_candidates(connection, candidates)
                self._checkpoint("candidates")
                for order in orders:
                    self.database._write_order(connection, order)
                self._checkpoint("orders")
                self.database._write_job(
                    connection,
                    job.job_run_id,
                    job.job_name,
                    job.started_at,
                    job.status,
                    job.message,
                    job.finished_at,
                    job.scheduled_for,
                    job.metadata,
                )
                self.database._complete_job_lease(connection, job.job_run_id, job.finished_at)
                self._checkpoint("job")
                connection.execute("COMMIT")
                LOGGER.info(
                    "research run committed run_id=%s state=%s candidates=%s orders=%s job_run_id=%s",
                    run.run_id,
                    run.lifecycle_state.value,
                    len(candidates),
                    len(orders),
                    job.job_run_id,
                )
                return True
            except Exception:
                connection.execute("ROLLBACK")
                LOGGER.exception("research run transaction rolled back run_id=%s", run.run_id)
                raise
