from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import duckdb
import pytest

from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database, JobAlreadyRunningError, JobLeaseLostError
from kfcquant.models import CandidateOutcome, EvaluationStatus, OpportunityOutcome, SignalKind
from tests.conftest import strategy_attribution
from tests.test_ops import ops_settings


def test_expired_job_is_recovered_once_and_same_job_can_restart(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    started = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    lease = timedelta(minutes=5)

    database.start_job("crashed", "run-preclose", started, lease)

    assert database.recover_expired_jobs(started + timedelta(minutes=4)) == []
    assert database.recover_expired_jobs(started + timedelta(minutes=6)) == ["crashed"]
    assert database.recover_expired_jobs(started + timedelta(minutes=7)) == []
    crashed = database.latest_job("run-preclose")
    assert crashed["status"] == "failed"
    assert crashed["finished_at"] == started + timedelta(minutes=6)
    assert "lease expired" in crashed["message"]
    lease_row = database.table("job_leases").iloc[0]
    assert lease_row["recovery_count"] == 1

    restarted = started + timedelta(minutes=8)
    database.start_job("retry", "run-preclose", restarted, lease)
    database.finish_job("retry", restarted + timedelta(minutes=1), "success", "retry complete")
    assert database.latest_job("run-preclose")["job_run_id"] == "retry"
    assert database.latest_job("run-preclose")["status"] == "success"


def test_active_lease_is_renewed_and_fences_competing_or_late_completion(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    started = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    lease = timedelta(minutes=5)
    database.start_job("active", "sync-eod", started, lease)

    with pytest.raises(JobAlreadyRunningError, match="sync-eod"):
        database.start_job("competitor", "sync-eod", started + timedelta(minutes=1), lease)

    assert database.heartbeat_job("active", started + timedelta(minutes=4), lease)
    assert database.recover_expired_jobs(started + timedelta(minutes=6)) == []
    assert database.recover_expired_jobs(started + timedelta(minutes=10)) == ["active"]
    assert not database.heartbeat_job("active", started + timedelta(minutes=11), lease)
    with pytest.raises(JobLeaseLostError, match="active"):
        database.finish_job("active", started + timedelta(minutes=11), "success", "too late")


def test_competing_workers_atomically_acquire_only_one_job_lease(settings):
    first = Database(settings.database_path, settings.initial_cash)
    second = Database(settings.database_path, settings.initial_cash)
    first.initialize()
    started = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    barrier = Barrier(2)

    def attempt(database, job_run_id):
        barrier.wait()
        try:
            database.start_job(job_run_id, "run-preclose", started, timedelta(minutes=5))
            return "started"
        except JobAlreadyRunningError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: attempt(*item),
                [(first, "worker-a"), (second, "worker-b")],
            )
        )

    assert sorted(results) == ["blocked", "started"]
    assert len(first.table("job_runs")) == 1
    assert len(first.table("job_leases")) == 1


def test_operations_only_blocks_on_a_live_or_unverifiable_job_lease(settings, tmp_path):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    started = datetime.now(SHANGHAI_TZ) - timedelta(minutes=10)
    database.start_job("expired", "run-postclose", started, timedelta(minutes=1))

    configured = ops_settings(tmp_path)
    configured.research_database = settings.database_path
    configured.research_lock = settings.database_path.with_suffix(".lock")
    manager = DeploymentManager(configured, OpsStore(configured.database_path))

    assert not manager._research_job_running()

    database.start_job("live", "run-preclose", datetime.now(SHANGHAI_TZ), timedelta(minutes=5))
    assert manager._research_job_running()

    with database.connect() as connection:
        connection.execute("DELETE FROM job_leases WHERE job_run_id='live'")
    assert manager._research_job_running()


def test_atomic_outcome_and_report_upserts_preserve_old_rows_on_conflict(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 20, 30, tzinfo=SHANGHAI_TZ)

    opportunity_a = OpportunityOutcome(
        **strategy_attribution(),
        outcome_id="op-a",
        position_id="position-a",
        ts_code="600000.SH",
        entry_date=date(2026, 8, 7),
        first_day_hit=False,
        five_day_hit=False,
        holding_days=1,
        net_return=0,
        recorded_at=at,
    )
    opportunity_b = opportunity_a.model_copy(update={"outcome_id": "op-b", "position_id": "position-b"})
    database.save_outcome(opportunity_a)
    database.save_outcome(opportunity_b)
    with pytest.raises(duckdb.ConstraintException):
        database.save_outcome(opportunity_b.model_copy(update={"outcome_id": "op-a", "net_return": 1}))
    opportunities = database.table("opportunity_outcomes").sort_values("position_id")
    assert opportunities[["outcome_id", "position_id", "net_return"]].to_dict("records") == [
        {"outcome_id": "op-a", "position_id": "position-a", "net_return": 0.0},
        {"outcome_id": "op-b", "position_id": "position-b", "net_return": 0.0},
    ]
    database.save_outcome(opportunity_a.model_copy(update={"outcome_id": "op-c", "net_return": 0.1}))
    updated_opportunity = database.table("opportunity_outcomes").query("position_id == 'position-a'").iloc[0]
    assert updated_opportunity["outcome_id"] == "op-c"
    assert updated_opportunity["net_return"] == pytest.approx(0.1)

    candidate_a = CandidateOutcome(
        **strategy_attribution(),
        outcome_id="candidate-a",
        run_id="run-a",
        ts_code="600000.SH",
        signal_kind=SignalKind.PRECLOSE_ENTRY,
        status=EvaluationStatus.MISS,
        evaluated_at=at,
    )
    candidate_b = candidate_a.model_copy(update={"outcome_id": "candidate-b", "run_id": "run-b"})
    database.save_candidate_outcome(candidate_a)
    database.save_candidate_outcome(candidate_b)
    with pytest.raises(duckdb.ConstraintException):
        database.save_candidate_outcome(candidate_b.model_copy(update={"outcome_id": "candidate-a"}))
    candidates = database.table("candidate_outcomes").sort_values("run_id")
    assert candidates[["outcome_id", "run_id", "status"]].to_dict("records") == [
        {"outcome_id": "candidate-a", "run_id": "run-a", "status": "miss"},
        {"outcome_id": "candidate-b", "run_id": "run-b", "status": "miss"},
    ]
    database.save_candidate_outcome(
        candidate_a.model_copy(update={"outcome_id": "candidate-c", "status": EvaluationStatus.HIT})
    )
    updated_candidate = database.table("candidate_outcomes").query("run_id == 'run-a'").iloc[0]
    assert updated_candidate["outcome_id"] == "candidate-c"
    assert updated_candidate["status"] == "hit"

    database.save_report("report-a", at.date(), at, "postclose", "old", "fixture")
    database.save_report("report-b", date(2026, 8, 11), at, "postclose", "other", "fixture")
    with pytest.raises(duckdb.ConstraintException):
        database.save_report("report-a", date(2026, 8, 11), at, "postclose", "new", "fixture")
    reports = database.table("reports").sort_values("report_date")
    assert reports[["report_id", "content"]].to_dict("records") == [
        {"report_id": "report-a", "content": "old"},
        {"report_id": "report-b", "content": "other"},
    ]
    database.save_report("report-c", at.date(), at, "postclose", "updated", "fixture-v2")
    updated_report = database.table("reports").query("report_id == 'report-c'").iloc[0]
    assert updated_report["report_id"] == "report-c"
    assert updated_report["content"] == "updated"
