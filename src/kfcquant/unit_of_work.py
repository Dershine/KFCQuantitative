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
from kfcquant.run_manifest import ResearchRunManifest, candidate_result_sha256
from kfcquant.strategy_identity import canonical_parameter_json

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
        manifest: ResearchRunManifest | None = None,
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
        manifest: ResearchRunManifest | None,
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
        run_attribution = (
            run.strategy_id,
            run.strategy_version,
            run.parameter_hash,
            canonical_parameter_json(run.strategy_parameters),
        )
        if any(
            (
                order.strategy_id,
                order.strategy_version,
                order.parameter_hash,
                canonical_parameter_json(order.strategy_parameters),
            )
            != run_attribution
            for order in orders
        ):
            raise ValueError("every order must retain the published run strategy attribution")
        if job.status != run.status.value:
            raise ValueError("job completion status must match the research run result")
        if run.lifecycle_state != ResearchRunState.PUBLISHED:
            if candidates or orders or run.tradable or manifest is not None:
                raise ValueError("failed or missed runs cannot publish candidates or orders")
            return
        if manifest is None:
            raise ValueError("a published run requires a complete run manifest")
        manifest = ResearchRunManifest.model_validate_json(manifest.model_dump_json())
        expected_manifest_identity = (
            run.run_id,
            run.signal_kind,
            run.information_cutoff or run.as_of,
            run.strategy_id,
            run.strategy_version,
            run.parameter_hash,
            canonical_parameter_json(run.strategy_parameters),
        )
        actual_manifest_identity = (
            manifest.run_id,
            manifest.signal_kind,
            manifest.information_cutoff,
            manifest.strategy_id,
            manifest.strategy_version,
            manifest.parameter_hash,
            canonical_parameter_json(manifest.strategy_parameters),
        )
        if actual_manifest_identity != expected_manifest_identity:
            raise ValueError("run manifest identity must match the published run")
        if any(snapshot.information_cutoff != manifest.information_cutoff for snapshot in manifest.input_snapshots):
            raise ValueError("run manifest input snapshots must share the run information cutoff")
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
        if manifest.result_sha256 != candidate_result_sha256(candidates):
            raise ValueError("run manifest result hash must match the published candidates")

    @staticmethod
    def _is_identical_existing_publication(
        connection: duckdb.DuckDBPyConnection,
        run: SignalRun,
        candidates: list[CandidateScore],
        orders: list[PaperOrder],
        job: JobCompletion,
        manifest: ResearchRunManifest | None,
    ) -> bool:
        stored = connection.execute(
            """SELECT r.status, r.lifecycle_state, r.tradable, r.candidate_count,
                      a.strategy_id, a.strategy_version, a.parameter_hash, a.parameter_snapshot_json
               FROM signal_runs r JOIN strategy_attributions a
                 ON a.entity_kind='signal_run' AND a.entity_id=r.run_id
               WHERE r.run_id=?""",
            [run.run_id],
        ).fetchone()
        if stored is None:
            return False
        expected_run = (
            run.status.value,
            run.lifecycle_state.value,
            run.tradable,
            run.candidate_count,
            run.strategy_id,
            run.strategy_version,
            run.parameter_hash,
            canonical_parameter_json(run.strategy_parameters),
        )
        stored_candidates = connection.execute(
            "SELECT ts_code, rank, blocked, opportunity_score FROM candidate_scores WHERE run_id=? ORDER BY ts_code",
            [run.run_id],
        ).fetchall()
        expected_candidates = sorted(
            (candidate.ts_code, candidate.rank, candidate.blocked, candidate.opportunity_score)
            for candidate in candidates
        )
        stored_orders = connection.execute(
            """SELECT o.order_id, o.ts_code, o.side, o.status, o.target_value,
                      a.strategy_id, a.strategy_version, a.parameter_hash, a.parameter_snapshot_json
               FROM paper_orders o JOIN strategy_attributions a
                 ON a.entity_kind='paper_order' AND a.entity_id=o.order_id
               WHERE o.run_id=? ORDER BY o.order_id""",
            [run.run_id],
        ).fetchall()
        expected_orders = sorted(
            (
                order.order_id,
                order.ts_code,
                order.side.value,
                order.status.value,
                order.target_value,
                order.strategy_id,
                order.strategy_version,
                order.parameter_hash,
                canonical_parameter_json(order.strategy_parameters),
            )
            for order in orders
        )
        stored_job = connection.execute(
            "SELECT job_name, status, message FROM job_runs WHERE job_run_id=?",
            [job.job_run_id],
        ).fetchone()
        expected_job = (job.job_name, job.status, job.message)
        stored_manifest = connection.execute(
            "SELECT manifest_sha256 FROM run_manifests WHERE run_id=?",
            [run.run_id],
        ).fetchone()
        expected_manifest = (manifest.manifest_sha256,) if manifest is not None else None
        if (
            stored == expected_run
            and stored_candidates == expected_candidates
            and stored_orders == expected_orders
            and stored_job == expected_job
            and stored_manifest == expected_manifest
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
        manifest: ResearchRunManifest | None = None,
    ) -> bool:
        self._validate(run, candidates, orders, job, manifest)
        with self.database.connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                if self._is_identical_existing_publication(connection, run, candidates, orders, job, manifest):
                    connection.execute("COMMIT")
                    return False
                self.database._assert_active_job_lease(connection, job.job_run_id, job.finished_at)
                self.database._write_signal_run(connection, run)
                self._checkpoint("run")
                if manifest is not None:
                    self.database._write_run_manifest(connection, manifest)
                self._checkpoint("manifest")
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
