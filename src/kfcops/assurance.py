from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import duckdb
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from kfcops.config import OpsSettings
from kfcops.store import OpsStore

REQUIRED_RESEARCH_TABLES = frozenset(
    {"schema_migrations", "paper_account", "signal_runs", "candidate_scores", "job_runs"}
)
CAPACITY_REPORT_VERSION = 2
CAPACITY_JOB_SAMPLE_POLICY = "successful_or_degraded_jobs_v1"
CAPACITY_ELIGIBLE_JOB_STATUSES = frozenset({"success", "degraded"})


class CapacityRecommendation(StrEnum):
    CONTINUE_DUCKDB = "continue_duckdb"
    COLLECT_MORE_EVIDENCE = "collect_more_evidence"
    OPTIMIZE_QUERIES = "optimize_queries"
    OPTIMIZE_WORKLOAD = "optimize_workload"
    EVALUATE_STORAGE_CONCURRENCY = "evaluate_storage_concurrency"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    ordered = sorted(value for value in values if math.isfinite(value) and value >= 0)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return ordered[index]

    return {
        "count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1] if ordered else None,
    }


def _report_hash_valid(payload: dict[str, Any]) -> bool:
    expected = payload.get("record_sha256")
    if not isinstance(expected, str):
        return False
    unsigned = {key: value for key, value in payload.items() if key != "record_sha256"}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest() == expected


class AssuranceManager:
    def __init__(
        self,
        settings: OpsSettings,
        store: OpsStore,
        *,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timer = timer or perf_counter

    def _write_report(self, category: str, payload: dict[str, Any]) -> Path:
        directory = self.settings.assurance_directory / category
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = self.clock().strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"{timestamp}-{uuid4().hex}.json"
        payload["report_path"] = str(target)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def _resolve_backup(self, backup: Path | None) -> Path:
        root = self.settings.backup_directory.resolve(strict=False)
        if backup is None:
            candidates = sorted(
                root.glob("*.duckdb"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise FileNotFoundError("backup_directory contains no DuckDB backup")
            selected = candidates[0]
        else:
            selected = backup.resolve(strict=False)
        try:
            selected.relative_to(root)
        except ValueError as exc:
            raise ValueError("backup must be inside configured backup_directory") from exc
        if not selected.is_file():
            raise FileNotFoundError(f"backup does not exist: {selected}")
        return selected

    @staticmethod
    def _database_health(database: Path) -> dict[str, Any]:
        with duckdb.connect(str(database), read_only=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
            missing = sorted(REQUIRED_RESEARCH_TABLES - tables)
            if missing:
                raise RuntimeError(f"restored database is missing required tables: {', '.join(missing)}")
            schema_row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
            schema_version = int(schema_row[0] or 0)
            if schema_version <= 0:
                raise RuntimeError("restored database has no applied schema version")
            counts = {
                table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
                for table in sorted(REQUIRED_RESEARCH_TABLES)
            }
            connection.execute("SELECT 1").fetchone()
        return {
            "duckdb_read_only_open": True,
            "schema_version": schema_version,
            "required_tables_ok": True,
            "row_counts": counts,
        }

    def run_recovery_drill(self, backup: Path | None = None) -> dict[str, Any]:
        started_at = self.clock()
        started = self.timer()
        report: dict[str, Any] = {
            "report_version": 1,
            "drill_type": "isolated_duckdb_backup_restore",
            "started_at": started_at.isoformat(),
            "status": "failed",
            "source_database_modified": False,
            "backup_modified": False,
        }
        selected: Path | None = None
        try:
            with FileLock(self.settings.deployment_lock, timeout=0):
                selected = self._resolve_backup(backup)
                before_hash = _sha256(selected)
                report.update({"backup_path": str(selected), "backup_sha256": before_hash})
                self.settings.assurance_directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix=".recovery-",
                    dir=self.settings.assurance_directory,
                ) as directory:
                    restored = Path(directory) / "restored.duckdb"
                    shutil.copy2(selected, restored)
                    restored_hash = _sha256(restored)
                    if restored_hash != before_hash:
                        raise RuntimeError("restored copy hash differs from backup")
                    report["restored_sha256"] = restored_hash
                    report["health"] = self._database_health(restored)
                if _sha256(selected) != before_hash:
                    raise RuntimeError("backup changed during recovery drill")
                report["status"] = "passed"
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc)
        report["finished_at"] = self.clock().isoformat()
        report["duration_seconds"] = max(0.0, self.timer() - started)
        path = self._write_report("recovery-drills", report)
        result = str(report["status"])
        self.store.audit("recovery-drill", str(selected or backup or "latest"), result, str(path))
        return report

    def _read_metrics(self) -> tuple[dict[str, list[float]], int]:
        selected: dict[str, list[float]] = {
            "job_duration_seconds": [],
            "database_lock_wait_seconds": [],
        }
        invalid = 0
        if not self.settings.metrics_path.is_file():
            return selected, invalid
        with self.settings.metrics_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    metric = str(record.get("metric", ""))
                    value = float(record["value"])
                    if record.get("record_type") != "metric" or metric not in selected:
                        continue
                    if metric == "database_lock_wait_seconds":
                        labels = record.get("labels") or {}
                        if isinstance(labels, dict) and labels.get("outcome") == "timeout":
                            continue
                    selected[metric].append(value)
                    if metric == "job_duration_seconds":
                        labels = record.get("labels") or {}
                        job_name = labels.get("job_name") if isinstance(labels, dict) else None
                        status = labels.get("status") if isinstance(labels, dict) else None
                        if (
                            isinstance(job_name, str)
                            and job_name
                            and status in CAPACITY_ELIGIBLE_JOB_STATUSES
                        ):
                            selected.setdefault(f"job_duration_seconds:{job_name}", []).append(value)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    invalid += 1
        return selected, invalid

    def _benchmark_queries(self, samples: int) -> tuple[dict[str, dict[str, int | float | None]], float]:
        queries = {
            "latest_signal": "SELECT * FROM signal_runs ORDER BY as_of DESC LIMIT 100",
            "open_positions": "SELECT * FROM paper_positions WHERE status='open' LIMIT 100",
            "recent_jobs": "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT 100",
        }
        timings = {name: [] for name in queries}
        lock_started = self.timer()
        try:
            with FileLock(self.settings.research_lock, timeout=5):
                lock_wait = max(0.0, self.timer() - lock_started)
                with duckdb.connect(str(self.settings.research_database), read_only=True) as connection:
                    for _ in range(samples):
                        for name, statement in queries.items():
                            started = self.timer()
                            connection.execute(statement).fetchall()
                            timings[name].append(max(0.0, self.timer() - started))
        except FileLockTimeout as exc:
            raise RuntimeError("capacity baseline could not acquire research database lock") from exc
        return {name: _distribution(values) for name, values in timings.items()}, lock_wait

    def _latest_recovery(self) -> dict[str, Any]:
        directory = self.settings.assurance_directory / "recovery-drills"
        reports = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not reports:
            return {"latest_status": "missing", "duration_seconds": None}
        try:
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not _report_hash_valid(report):
                raise ValueError("recovery report integrity check failed")
            return {
                "latest_status": report.get("status", "invalid"),
                "duration_seconds": report.get("duration_seconds"),
                "report_path": str(reports[0]),
            }
        except (OSError, ValueError, json.JSONDecodeError):
            return {"latest_status": "invalid", "duration_seconds": None, "report_path": str(reports[0])}

    def collect_capacity_baseline(self, *, query_samples: int | None = None) -> dict[str, Any]:
        samples = (
            query_samples
            if query_samples is not None
            else self.settings.capacity_minimum_query_samples
        )
        if samples < 1:
            raise ValueError("query_samples must be positive")
        started = self.timer()
        metric_values, invalid = self._read_metrics()
        report: dict[str, Any] = {
            "report_version": CAPACITY_REPORT_VERSION,
            "job_sample_policy": CAPACITY_JOB_SAMPLE_POLICY,
            "collected_at": self.clock().isoformat(),
            "status": "partial",
            "metrics": {name: _distribution(values) for name, values in metric_values.items()},
            "queries": {},
            "invalid_metric_lines": invalid,
            "recovery": self._latest_recovery(),
            "storage": {
                "database_bytes": self.settings.research_database.stat().st_size
                if self.settings.research_database.is_file()
                else 0,
                "parquet_bytes": sum(
                    item.stat().st_size
                    for item in self.settings.raw_data_directory.rglob("*.parquet")
                    if item.is_file()
                )
                if self.settings.raw_data_directory.exists()
                else 0,
            },
        }
        try:
            if not self.settings.research_database.is_file():
                raise FileNotFoundError("research database is missing")
            queries, lock_wait = self._benchmark_queries(samples)
            report["queries"] = queries
            report["baseline_lock_acquisition_seconds"] = lock_wait
            metric_values["database_lock_wait_seconds"].append(lock_wait)
            report["metrics"]["database_lock_wait_seconds"] = _distribution(
                metric_values["database_lock_wait_seconds"]
            )
            report["status"] = "complete"
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc)
        report["duration_seconds"] = max(0.0, self.timer() - started)
        path = self._write_report("capacity-baselines", report)
        self.store.audit("capacity-baseline", str(path), str(report["status"]), "read-only baseline")
        return report

    def _thresholds(self) -> dict[str, int | float]:
        return {
            "minimum_job_samples": self.settings.capacity_minimum_job_samples,
            "minimum_lock_samples": self.settings.capacity_minimum_lock_samples,
            "minimum_query_samples": self.settings.capacity_minimum_query_samples,
            "signal_runtime_budget_seconds": self.settings.capacity_signal_runtime_budget_seconds,
            "lock_wait_p95_limit_seconds": self.settings.capacity_lock_wait_p95_limit_seconds,
            "query_p95_limit_seconds": self.settings.capacity_query_p95_limit_seconds,
            "recovery_rto_seconds": self.settings.capacity_recovery_rto_seconds,
        }

    def evaluate_capacity(
        self,
        baseline: dict[str, Any],
        *,
        multiple_writers_required: bool = False,
        remote_transactions_required: bool = False,
    ) -> dict[str, Any]:
        thresholds = self._thresholds()
        metrics = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}
        queries = baseline.get("queries") if isinstance(baseline.get("queries"), dict) else {}
        recovery = baseline.get("recovery") if isinstance(baseline.get("recovery"), dict) else {}
        job = metrics.get("job_duration_seconds:run-preclose", {})
        lock = metrics.get("database_lock_wait_seconds", {})
        missing: list[str] = []
        if (
            baseline.get("report_version") != CAPACITY_REPORT_VERSION
            or baseline.get("job_sample_policy") != CAPACITY_JOB_SAMPLE_POLICY
        ):
            missing.append("job_sample_policy")
        if baseline.get("record_sha256") is not None and not _report_hash_valid(baseline):
            missing.append("baseline_record_integrity")
        if int(job.get("count", 0)) < self.settings.capacity_minimum_job_samples:
            missing.append("job_duration_samples")
        if int(lock.get("count", 0)) < self.settings.capacity_minimum_lock_samples:
            missing.append("database_lock_samples")
        for name in ("latest_signal", "open_positions", "recent_jobs"):
            query = queries.get(name, {})
            if int(query.get("count", 0)) < self.settings.capacity_minimum_query_samples:
                missing.append(f"query_samples:{name}")
        if recovery.get("latest_status") != "passed":
            missing.append("successful_recovery_drill")

        recommendation = CapacityRecommendation.CONTINUE_DUCKDB
        reasons: list[str] = []
        if missing:
            recommendation = CapacityRecommendation.COLLECT_MORE_EVIDENCE
            reasons.append("minimum evidence gate is not satisfied")
        elif multiple_writers_required or remote_transactions_required:
            recommendation = CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY
            reasons.append("a declared topology requirement exceeds the single-writer boundary")
        elif float(recovery.get("duration_seconds") or 0) > self.settings.capacity_recovery_rto_seconds:
            recommendation = CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY
            reasons.append("verified restore duration exceeds the recovery objective")
        elif (
            float(job.get("p95") or 0) > self.settings.capacity_signal_runtime_budget_seconds
            and float(lock.get("p95") or 0) > self.settings.capacity_lock_wait_p95_limit_seconds
        ):
            recommendation = CapacityRecommendation.EVALUATE_STORAGE_CONCURRENCY
            reasons.append("signal runtime and lock wait both exceed their p95 limits")
        elif float(job.get("p95") or 0) > self.settings.capacity_signal_runtime_budget_seconds:
            recommendation = CapacityRecommendation.OPTIMIZE_WORKLOAD
            reasons.append("signal runtime is slow without evidence that lock contention is causal")
        elif any(
            float(item.get("p95") or 0) > self.settings.capacity_query_p95_limit_seconds
            for item in queries.values()
            if isinstance(item, dict)
        ):
            recommendation = CapacityRecommendation.OPTIMIZE_QUERIES
            reasons.append("dashboard query p95 exceeds its limit without a storage-concurrency trigger")
        else:
            reasons.append("measured workload remains within the modular-monolith and DuckDB limits")

        report: dict[str, Any] = {
            "decision_version": 1,
            "decided_at": self.clock().isoformat(),
            "recommendation": recommendation.value,
            "reasons": reasons,
            "missing_evidence": missing,
            "thresholds": thresholds,
            "declared_requirements": {
                "multiple_writers_required": multiple_writers_required,
                "remote_transactions_required": remote_transactions_required,
            },
            "baseline_record_sha256": baseline.get("record_sha256"),
            "architecture_changed": False,
        }
        path = self._write_report("capacity-decisions", report)
        self.store.audit("capacity-decision", str(path), recommendation.value, "; ".join(reasons))
        return report
