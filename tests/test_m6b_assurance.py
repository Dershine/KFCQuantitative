from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from filelock import FileLock

from kfcops.assurance import (
    CAPACITY_JOB_SAMPLE_POLICY,
    CAPACITY_REPORT_VERSION,
    AssuranceManager,
    CapacityRecommendation,
)
from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore
from kfcops.supply_chain import scan_paths
from kfcquant.db import MIGRATIONS, Database


def m6b_settings(tmp_path: Path) -> OpsSettings:
    return OpsSettings(
        database_path=tmp_path / "ops.sqlite3",
        deployment_lock=tmp_path / "deploy.lock",
        repository_directory=tmp_path / "repository",
        releases_directory=tmp_path / "releases",
        current_release_link=tmp_path / "current",
        builder_python=Path(os.sys.executable),
        research_database=tmp_path / "research.duckdb",
        research_lock=tmp_path / "database.lock",
        backup_directory=tmp_path / "backups",
        assurance_directory=tmp_path / "assurance",
        metrics_path=tmp_path / "runtime" / "observability-metrics.jsonl",
        raw_data_directory=tmp_path / "raw",
        capacity_minimum_job_samples=2,
        capacity_minimum_lock_samples=2,
        capacity_minimum_query_samples=2,
        github_repository="owner/repository",
        session_secret="test-secret-that-is-at-least-32-bytes",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_metric(path: Path, metric: str, value: float, **labels: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "record_type": "metric",
                    "timestamp": "2026-08-19T00:00:00+00:00",
                    "metric": metric,
                    "value": value,
                    "labels": labels,
                }
            )
            + "\n"
        )


def prepare_backup(settings: OpsSettings) -> Path:
    database = Database(settings.research_database, lock_path=settings.research_lock)
    database.initialize()
    settings.backup_directory.mkdir(parents=True, exist_ok=True)
    backup = settings.backup_directory / "20260819010000-fixture.duckdb"
    shutil.copy2(settings.research_database, backup)
    return backup


def test_recovery_drill_uses_an_isolated_copy_and_records_health_without_mutating_sources(tmp_path):
    settings = m6b_settings(tmp_path)
    backup = prepare_backup(settings)
    source_hash = sha256(settings.research_database)
    backup_hash = sha256(backup)
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    report = manager.run_recovery_drill(backup)

    assert report["status"] == "passed"
    assert report["backup_sha256"] == backup_hash
    assert report["restored_sha256"] == backup_hash
    assert report["health"]["schema_version"] == len(MIGRATIONS)
    assert report["health"]["required_tables_ok"] is True
    assert sha256(settings.research_database) == source_hash
    assert sha256(backup) == backup_hash
    assert Path(report["report_path"]).is_file()
    assert not list(settings.assurance_directory.glob(".recovery-*"))
    audit = OpsStore(settings.database_path).recent_audit(1)[0]
    assert audit["action"] == "recovery-drill"
    assert audit["result"] == "passed"


def test_recovery_drill_fails_closed_for_corrupt_or_out_of_scope_backup_and_keeps_evidence(tmp_path):
    settings = m6b_settings(tmp_path)
    settings.backup_directory.mkdir(parents=True)
    corrupt = settings.backup_directory / "corrupt.duckdb"
    corrupt.write_bytes(b"not-a-duckdb")
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    corrupt_report = manager.run_recovery_drill(corrupt)
    outside_report = manager.run_recovery_drill(tmp_path / "outside.duckdb")

    assert corrupt_report["status"] == "failed"
    assert corrupt_report["error_type"]
    assert Path(corrupt_report["report_path"]).is_file()
    assert outside_report["status"] == "failed"
    assert "backup_directory" in outside_report["error"]
    assert not list(settings.assurance_directory.glob(".recovery-*"))


def test_recovery_drill_selects_latest_backup_and_fails_closed_when_none_exist(tmp_path):
    settings = m6b_settings(tmp_path)
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    missing = manager.run_recovery_drill()
    backup = prepare_backup(settings)
    selected = manager.run_recovery_drill()

    assert missing["status"] == "failed"
    assert missing["error_type"] == "FileNotFoundError"
    assert selected["status"] == "passed"
    assert Path(selected["backup_path"]) == backup.resolve()


def test_recovery_drill_refuses_to_race_an_active_deployment(tmp_path):
    settings = m6b_settings(tmp_path)
    prepare_backup(settings)
    manager = AssuranceManager(settings, OpsStore(settings.database_path))
    deployment_lock = FileLock(settings.deployment_lock, timeout=0)
    deployment_lock.acquire()
    try:
        report = manager.run_recovery_drill()
    finally:
        deployment_lock.release()

    assert report["status"] == "failed"
    assert report["error_type"] == "Timeout"


def test_new_release_manifest_binds_source_dependencies_migrations_and_verified_workflow(tmp_path, monkeypatch):
    settings = m6b_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    sha = "a" * 40
    release = settings.releases_directory / sha
    executable = release / ".venv" / ("Scripts/kfcquant.exe" if os.name == "nt" else "bin/kfcquant")
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture", encoding="utf-8")
    (release / "requirements.lock").write_text("duckdb==1.5.5\n", encoding="utf-8")
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: "2026-08-19T00:00:00+08:00")
    monkeypatch.setattr(
        manager,
        "_run_command",
        lambda command, **kwargs: (
            "duckdb==1.5.5\nkfcquant==0.2.0\n"
            if "freeze" in command
            else json.dumps({"contract_version": 1, "latest_schema_version": len(MIGRATIONS)})
            if "migration-contract" in command
            else "Python 3.12.11\n"
        ),
    )
    monkeypatch.setattr(manager, "_release_source_is_clean", lambda *args: True)

    manager._write_release(
        release,
        sha,
        workflow={"id": 1234, "url": "https://github.test/actions/runs/1234", "conclusion": "success"},
    )

    manifest = json.loads((release / ".release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["source_sha"] == sha
    assert manifest["requirements_lock_sha256"] == sha256(release / "requirements.lock")
    assert manifest["installed_packages_sha256"]
    assert manifest["migration_contract_sha256"]
    assert manifest["workflow"]["id"] == 1234
    assert manager._valid_release(release, sha) is True

    (release / "requirements.lock").write_text("duckdb==0.0.0\n", encoding="utf-8")
    assert manager._valid_release(release, sha) is False
    (release / "requirements.lock").write_text("duckdb==1.5.5\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="verifiable"):
        manager._write_release(release, sha, workflow={"conclusion": "success"})


def test_secret_scanner_rejects_high_confidence_secrets_and_allows_documented_placeholders(tmp_path):
    safe = tmp_path / "safe.env.example"
    unsafe = tmp_path / "unsafe.env"
    safe.write_text("LLM_API_KEY=your-key-here\nKFCOPS_SESSION_SECRET=change-me\n", encoding="utf-8")
    unsafe.write_text("LLM_API_KEY=sk-" + "A" * 32 + "\n", encoding="utf-8")

    findings = scan_paths([safe, unsafe])

    assert [finding.path for finding in findings] == [unsafe]
    assert findings[0].line_number == 1
    assert "secret" not in findings[0].matched_text.lower()

    allowlisted = tmp_path / "allowlisted.txt"
    allowlisted.write_text("-----BEGIN PRIVATE KEY----- # pragma: allowlist secret\n", encoding="utf-8")
    assert scan_paths([allowlisted, tmp_path / "missing.txt"]) == []


def test_capacity_baseline_aggregates_metrics_queries_sizes_and_recovery_evidence(tmp_path):
    settings = m6b_settings(tmp_path)
    backup = prepare_backup(settings)
    AssuranceManager(settings, OpsStore(settings.database_path)).run_recovery_drill(backup)
    write_metric(settings.metrics_path, "job_duration_seconds", 10, job_name="run-preclose", status="success")
    write_metric(settings.metrics_path, "job_duration_seconds", 20, job_name="run-preclose", status="degraded")
    write_metric(settings.metrics_path, "job_duration_seconds", 1, job_name="run-preclose", status="failed")
    write_metric(settings.metrics_path, "job_duration_seconds", 2, job_name="run-preclose", status="missed")
    write_metric(settings.metrics_path, "job_duration_seconds", 3, job_name="run-preclose")
    write_metric(settings.metrics_path, "database_lock_wait_seconds", 0.01, outcome="acquired")
    write_metric(settings.metrics_path, "database_lock_wait_seconds", 0.02, outcome="acquired")
    with settings.metrics_path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
    settings.raw_data_directory.mkdir(parents=True)
    (settings.raw_data_directory / "fixture.parquet").write_bytes(b"parquet-size-fixture")
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    report = manager.collect_capacity_baseline(query_samples=3)

    assert report["status"] == "complete"
    assert report["report_version"] == CAPACITY_REPORT_VERSION
    assert report["job_sample_policy"] == CAPACITY_JOB_SAMPLE_POLICY
    assert report["metrics"]["job_duration_seconds"]["count"] == 5
    assert report["metrics"]["job_duration_seconds"]["p95"] == 20
    assert report["metrics"]["job_duration_seconds:run-preclose"]["count"] == 2
    assert report["metrics"]["job_duration_seconds:run-preclose"]["p95"] == 20
    assert report["metrics"]["database_lock_wait_seconds"]["count"] >= 2
    assert report["invalid_metric_lines"] == 1
    assert set(report["queries"]) == {"latest_signal", "open_positions", "recent_jobs"}
    assert all(item["count"] == 3 for item in report["queries"].values())
    assert report["storage"]["database_bytes"] > 0
    assert report["storage"]["parquet_bytes"] == len(b"parquet-size-fixture")
    assert report["recovery"]["latest_status"] == "passed"
    assert Path(report["report_path"]).is_file()


def test_capacity_decision_is_fail_closed_when_evidence_is_thin_and_quantitative_when_complete(tmp_path):
    settings = m6b_settings(tmp_path)
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    thin = manager.evaluate_capacity({"status": "partial", "metrics": {}, "queries": {}, "recovery": {}})
    assert thin["recommendation"] == CapacityRecommendation.COLLECT_MORE_EVIDENCE.value

    baseline = {
        "report_version": CAPACITY_REPORT_VERSION,
        "job_sample_policy": CAPACITY_JOB_SAMPLE_POLICY,
        "status": "complete",
        "metrics": {
            "job_duration_seconds": {"count": 2, "p95": 10.0},
            "job_duration_seconds:run-preclose": {"count": 2, "p95": 10.0},
            "database_lock_wait_seconds": {"count": 2, "p95": 0.01},
        },
        "queries": {
            "latest_signal": {"count": 2, "p95": 0.01},
            "open_positions": {"count": 2, "p95": 0.01},
            "recent_jobs": {"count": 2, "p95": 0.01},
        },
        "recovery": {"latest_status": "passed", "duration_seconds": 1.0},
    }
    stable = manager.evaluate_capacity(baseline)
    assert stable["recommendation"] == CapacityRecommendation.CONTINUE_DUCKDB.value

    legacy = {key: value for key, value in baseline.items() if key != "job_sample_policy"}
    legacy["report_version"] = CAPACITY_REPORT_VERSION - 1
    legacy_decision = manager.evaluate_capacity(legacy)
    assert legacy_decision["recommendation"] == CapacityRecommendation.COLLECT_MORE_EVIDENCE.value
    assert "job_sample_policy" in legacy_decision["missing_evidence"]

    baseline["metrics"]["job_duration_seconds:run-preclose"]["p95"] = (
        settings.capacity_signal_runtime_budget_seconds + 1
    )
    baseline["metrics"]["database_lock_wait_seconds"]["p95"] = settings.capacity_lock_wait_p95_limit_seconds + 1
    constrained = manager.evaluate_capacity(baseline)
    assert constrained["recommendation"] == CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY.value
    assert constrained["thresholds"]["signal_runtime_budget_seconds"] == settings.capacity_signal_runtime_budget_seconds

    baseline["metrics"]["database_lock_wait_seconds"]["p95"] = 0.01
    workload = manager.evaluate_capacity(baseline)
    assert workload["recommendation"] == CapacityRecommendation.OPTIMIZE_WORKLOAD.value

    baseline["metrics"]["job_duration_seconds:run-preclose"]["p95"] = 10.0
    baseline["queries"]["latest_signal"]["p95"] = settings.capacity_query_p95_limit_seconds + 1
    queries = manager.evaluate_capacity(baseline)
    assert queries["recommendation"] == CapacityRecommendation.OPTIMIZE_QUERIES.value

    baseline["queries"]["latest_signal"]["p95"] = 0.01
    topology = manager.evaluate_capacity(baseline, multiple_writers_required=True)
    assert topology["recommendation"] == CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY.value

    baseline["recovery"]["duration_seconds"] = settings.capacity_recovery_rto_seconds + 1
    recovery = manager.evaluate_capacity(baseline)
    assert recovery["recommendation"] == CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY.value


def test_capacity_baseline_records_missing_database_lock_failure_and_tampered_recovery(tmp_path):
    settings = m6b_settings(tmp_path)
    manager = AssuranceManager(settings, OpsStore(settings.database_path))

    missing = manager.collect_capacity_baseline(query_samples=1)
    assert missing["status"] == "partial"
    assert missing["error_type"] == "FileNotFoundError"
    with pytest.raises(ValueError, match="positive"):
        manager.collect_capacity_baseline(query_samples=0)

    prepare_backup(settings)
    recovery = manager.run_recovery_drill()
    recovery_path = Path(recovery["report_path"])
    tampered = json.loads(recovery_path.read_text(encoding="utf-8"))
    tampered["status"] = "failed"
    recovery_path.write_text(json.dumps(tampered), encoding="utf-8")
    lock = FileLock(settings.research_lock, timeout=0)
    lock.acquire()
    try:
        locked = manager.collect_capacity_baseline(query_samples=1)
    finally:
        lock.release()
    assert locked["status"] == "partial"
    assert locked["recovery"]["latest_status"] == "invalid"
    assert "lock" in locked["error"].lower()


def test_m6b_linux_contract_installs_weekly_non_destructive_recovery_drill():
    root = Path(__file__).parents[1]
    bootstrap = (root / "deploy" / "bootstrap_server.sh").read_text(encoding="utf-8")
    service = (root / "deploy" / "systemd" / "kfcquant-assurance.service").read_text(encoding="utf-8")
    timer = (root / "deploy" / "systemd" / "kfcquant-assurance.timer").read_text(encoding="utf-8")

    assert "kfcquant-assurance.service" in bootstrap
    assert "kfcquant-assurance.timer" in bootstrap
    assert "kfcops recovery-drill" in service
    assert "User=kfcops" in service
    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer
