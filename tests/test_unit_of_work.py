from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import (
    CandidateScore,
    FactorBreakdown,
    OrderSide,
    PaperOrder,
    ResearchRunState,
    RunStatus,
    SignalRun,
)
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork, JobCompletion


def publication_fixture():
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="atomic-run",
        as_of=at,
        status=RunStatus.SUCCESS,
        lifecycle_state=ResearchRunState.PUBLISHED,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
        candidate_count=1,
    )
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="600000.SH",
        name="fixture",
        rank=1,
        opportunity_score=80,
        factor_breakdown=FactorBreakdown(),
        quote_at=at,
    )
    order = PaperOrder(
        order_id="atomic-order",
        run_id=run.run_id,
        ts_code=candidate.ts_code,
        side=OrderSide.BUY,
        created_at=at,
        target_value=20_000,
        reason="fixture",
    )
    job = JobCompletion(
        job_run_id="atomic-job",
        job_name="run-preclose",
        started_at=at,
        finished_at=at,
        status="success",
        message="ok",
        metadata={"candidates": 1, "orders": 1},
    )
    return run, [candidate], [order], job


def prepare_database(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    run, candidates, orders, job = publication_fixture()
    database.record_job(job.job_run_id, job.job_name, job.started_at, "running", "started")
    return database, run, candidates, orders, job


def test_uow_commits_published_run_candidates_orders_and_job_together(settings):
    database, run, candidates, orders, job = prepare_database(settings)

    created = DuckDBResearchRunUnitOfWork(database).commit(run, candidates, orders, job)

    assert created
    assert database.latest_signal_run()["run_id"] == run.run_id
    assert database.get_candidates(run.run_id)["ts_code"].tolist() == ["600000.SH"]
    assert database.proposed_orders(run.run_id)["order_id"].tolist() == ["atomic-order"]
    assert database.latest_job("run-preclose")["status"] == "success"


@pytest.mark.parametrize("fail_after", ["run", "candidates", "orders", "job"])
def test_uow_fault_injection_rolls_back_every_publication_write(settings, fail_after):
    database, run, candidates, orders, job = prepare_database(settings)

    class FailingUnitOfWork(DuckDBResearchRunUnitOfWork):
        def _checkpoint(self, stage):
            if stage == fail_after:
                raise RuntimeError(f"injected after {stage}")

    with pytest.raises(RuntimeError, match=f"injected after {fail_after}"):
        FailingUnitOfWork(database).commit(run, candidates, orders, job)

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("candidate_scores").empty
    assert database.table("paper_orders").empty
    assert database.latest_job("run-preclose")["status"] == "running"


def test_repeated_identical_publication_is_idempotent(settings):
    database, run, candidates, orders, job = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)

    assert uow.commit(run, candidates, orders, job)
    assert not uow.commit(run, candidates, orders, job)

    assert len(database.table("signal_runs")) == 1
    assert len(database.table("candidate_scores")) == 1
    assert len(database.table("paper_orders")) == 1


def test_uow_rejects_orders_for_non_tradable_or_blocked_results(settings):
    database, run, candidates, orders, job = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)

    with pytest.raises(ValueError, match="non-tradable"):
        uow.commit(run.model_copy(update={"tradable": False}), candidates, orders, job)

    blocked = candidates[0].model_copy(update={"blocked": True})
    with pytest.raises(ValueError, match="blocked"):
        uow.commit(run.model_copy(update={"candidate_count": 0}), [blocked], orders, job)


def test_uow_rejects_inconsistent_publication_contracts(settings):
    database, run, candidates, orders, job = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)

    invalid_cases = [
        (
            run.model_copy(update={"lifecycle_state": ResearchRunState.STAGED, "status": RunStatus.RUNNING}),
            candidates,
            orders,
            replace(job, status="running"),
            "published, failed, or missed",
        ),
        (run, [candidates[0].model_copy(update={"run_id": "other"})], orders, job, "candidate must belong"),
        (run, candidates, [orders[0].model_copy(update={"run_id": "other"})], job, "order must belong"),
        (run, candidates, orders, replace(job, status="failed"), "job completion status"),
        (
            run.model_copy(update={"lifecycle_state": ResearchRunState.FAILED, "status": RunStatus.FAILED}),
            candidates,
            [],
            replace(job, status="failed"),
            "cannot publish candidates",
        ),
        (
            run.model_copy(update={"status": RunStatus.FAILED}),
            candidates,
            orders,
            replace(job, status="failed"),
            "successful or degraded",
        ),
        (run, candidates * 2, orders, job, "duplicate candidates"),
        (run.model_copy(update={"candidate_count": 2}), candidates, orders, job, "candidate_count"),
        (run, candidates, [orders[0].model_copy(update={"side": OrderSide.SELL})], job, "buy orders"),
        (run, candidates, orders * 2, job, "duplicate orders"),
    ]

    for invalid_run, invalid_candidates, invalid_orders, invalid_job, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            uow.commit(invalid_run, invalid_candidates, invalid_orders, invalid_job)


def test_published_run_is_immutable_after_first_commit(settings):
    database, run, candidates, orders, job = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)
    uow.commit(run, candidates, orders, job)

    changed = candidates[0].model_copy(update={"opportunity_score": 81})
    with pytest.raises(ValueError, match="immutable"):
        uow.commit(run, [changed], orders, job)
