from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database, JobLeaseLostError
from kfcquant.models import (
    CandidateScore,
    FactorBreakdown,
    OrderSide,
    PaperOrder,
    ResearchRunState,
    RunStatus,
    SignalRun,
)
from kfcquant.run_manifest import (
    ResearchRunManifest,
    RunInputKind,
    RunInputSnapshot,
    candidate_result_sha256,
)
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork, JobCompletion
from tests.conftest import strategy_attribution


def publication_fixture():
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    attribution = strategy_attribution()
    run = SignalRun(
        **attribution,
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
        **attribution,
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
    snapshots = tuple(
        RunInputSnapshot(
            snapshot_id=f"{index:x}" * 64,
            dataset_kind=kind,
            schema_version=f"{kind.value}-v1",
            source="fixture",
            captured_at=at,
            information_cutoff=at,
            snapshot_path=f"run-inputs/{kind.value}/{index:x}.parquet",
            content_sha256=f"{index:x}" * 64,
            row_count=1,
        )
        for index, kind in enumerate(
            (
                RunInputKind.SECURITY,
                RunInputKind.DAILY_BAR,
                RunInputKind.LIVE_QUOTE,
                RunInputKind.RISK_EVENT,
                RunInputKind.UNPROCESSED_OFFICIAL_CODE,
                RunInputKind.PREVIOUS_SIGNAL_CODE,
            ),
            start=1,
        )
    )
    manifest = ResearchRunManifest.create(
        run,
        snapshots,
        candidate_result_sha256([candidate]),
        source_sha="a" * 40,
        source_dirty=False,
        project_version="0.2.0",
        python_version="3.13.0",
        dependency_lock_sha256="b" * 64,
        created_at=at,
    )
    return run, [candidate], [order], job, manifest


def prepare_database(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    run, candidates, orders, job, manifest = publication_fixture()
    database.start_job(job.job_run_id, job.job_name, job.started_at, timedelta(minutes=5))
    return database, run, candidates, orders, job, manifest


def test_uow_commits_published_run_candidates_orders_and_job_together(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)

    created = DuckDBResearchRunUnitOfWork(database).commit(run, candidates, orders, job, manifest)

    assert created
    assert database.latest_signal_run()["run_id"] == run.run_id
    assert database.get_candidates(run.run_id)["ts_code"].tolist() == ["600000.SH"]
    assert database.proposed_orders(run.run_id)["order_id"].tolist() == ["atomic-order"]
    assert database.latest_job("run-preclose")["status"] == "success"
    assert database.get_run_manifest(run.run_id)["manifest"] == manifest


@pytest.mark.parametrize("fail_after", ["run", "manifest", "candidates", "orders", "job"])
def test_uow_fault_injection_rolls_back_every_publication_write(settings, fail_after):
    database, run, candidates, orders, job, manifest = prepare_database(settings)

    class FailingUnitOfWork(DuckDBResearchRunUnitOfWork):
        def _checkpoint(self, stage):
            if stage == fail_after:
                raise RuntimeError(f"injected after {stage}")

    with pytest.raises(RuntimeError, match=f"injected after {fail_after}"):
        FailingUnitOfWork(database).commit(run, candidates, orders, job, manifest)

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("candidate_scores").empty
    assert database.table("paper_orders").empty
    assert database.table("run_manifests").empty
    assert database.latest_job("run-preclose")["status"] == "running"


def test_repeated_identical_publication_is_idempotent(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)

    assert uow.commit(run, candidates, orders, job, manifest)
    assert not uow.commit(run, candidates, orders, job, manifest)

    assert len(database.table("signal_runs")) == 1
    assert len(database.table("candidate_scores")) == 1
    assert len(database.table("paper_orders")) == 1
    assert len(database.table("run_manifests")) == 1


def test_uow_rejects_orders_for_non_tradable_or_blocked_results(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)

    with pytest.raises(ValueError, match="non-tradable"):
        uow.commit(run.model_copy(update={"tradable": False}), candidates, orders, job, manifest)

    blocked = candidates[0].model_copy(update={"blocked": True})
    with pytest.raises(ValueError, match="blocked"):
        uow.commit(run.model_copy(update={"candidate_count": 0}), [blocked], orders, job, manifest)


def test_uow_rejects_inconsistent_publication_contracts(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
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
        (
            run,
            candidates,
            [orders[0].model_copy(update={"strategy_id": "other-strategy"})],
            job,
            "strategy attribution",
        ),
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
            uow.commit(invalid_run, invalid_candidates, invalid_orders, invalid_job, manifest)

    with pytest.raises(ValueError, match="manifest"):
        uow.commit(run, candidates, orders, job, None)
    with pytest.raises(ValueError, match="manifest"):
        uow.commit(run, candidates, orders, job, manifest.model_copy(update={"run_id": "other"}))

    other_run = run.model_copy(update={"run_id": "other"})
    other_manifest = ResearchRunManifest.create(
        other_run,
        manifest.input_snapshots,
        candidate_result_sha256(candidates),
        source_sha=manifest.source_sha,
        source_dirty=manifest.source_dirty,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        dependency_lock_sha256=manifest.dependency_lock_sha256,
        created_at=manifest.created_at,
    )
    with pytest.raises(ValueError, match="identity"):
        uow.commit(run, candidates, orders, job, other_manifest)

    shifted_snapshots = (
        manifest.input_snapshots[0].model_copy(
            update={"information_cutoff": manifest.information_cutoff - timedelta(seconds=1)}
        ),
        *manifest.input_snapshots[1:],
    )
    shifted_manifest = ResearchRunManifest.create(
        run,
        shifted_snapshots,
        candidate_result_sha256(candidates),
        source_sha=manifest.source_sha,
        source_dirty=manifest.source_dirty,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        dependency_lock_sha256=manifest.dependency_lock_sha256,
        created_at=manifest.created_at,
    )
    with pytest.raises(ValueError, match="share the run information cutoff"):
        uow.commit(run, candidates, orders, job, shifted_manifest)

    wrong_result_manifest = ResearchRunManifest.create(
        run,
        manifest.input_snapshots,
        candidate_result_sha256([]),
        source_sha=manifest.source_sha,
        source_dirty=manifest.source_dirty,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        dependency_lock_sha256=manifest.dependency_lock_sha256,
        created_at=manifest.created_at,
    )
    with pytest.raises(ValueError, match="result hash"):
        uow.commit(run, candidates, orders, job, wrong_result_manifest)


def test_published_run_is_immutable_after_first_commit(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
    uow = DuckDBResearchRunUnitOfWork(database)
    uow.commit(run, candidates, orders, job, manifest)

    changed = candidates[0].model_copy(update={"opportunity_score": 81})
    changed_manifest = ResearchRunManifest.create(
        run,
        manifest.input_snapshots,
        candidate_result_sha256([changed]),
        source_sha=manifest.source_sha,
        source_dirty=manifest.source_dirty,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        dependency_lock_sha256=manifest.dependency_lock_sha256,
        created_at=manifest.created_at,
    )
    with pytest.raises(ValueError, match="immutable"):
        uow.commit(run, [changed], orders, job, changed_manifest)


def test_expired_job_cannot_publish_a_research_run(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
    database.recover_expired_jobs(job.started_at + timedelta(minutes=6))

    with pytest.raises(JobLeaseLostError, match=job.job_run_id):
        DuckDBResearchRunUnitOfWork(database).commit(
            run,
            candidates,
            orders,
            replace(job, finished_at=job.started_at + timedelta(minutes=6)),
            manifest,
        )

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("candidate_scores").empty
    assert database.table("paper_orders").empty


def test_unknown_upstream_ingestion_batch_rolls_back_publication(settings):
    database, run, candidates, orders, job, manifest = prepare_database(settings)
    snapshots = list(manifest.input_snapshots)
    snapshots[2] = snapshots[2].model_copy(update={"ingestion_batch_ids": ("missing-batch",)})
    changed_manifest = ResearchRunManifest.create(
        run,
        tuple(snapshots),
        candidate_result_sha256(candidates),
        source_sha=manifest.source_sha,
        source_dirty=manifest.source_dirty,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        dependency_lock_sha256=manifest.dependency_lock_sha256,
        created_at=manifest.created_at,
    )

    with pytest.raises(ValueError, match="unknown ingestion batch"):
        DuckDBResearchRunUnitOfWork(database).commit(
            run, candidates, orders, job, changed_manifest
        )

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("run_manifests").empty
