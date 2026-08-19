from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from kfcquant import __version__
from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.db import Database
from kfcquant.observability import AlertCode, MetricName, Observability, get_observability


@lru_cache(maxsize=1)
def build_identity() -> dict[str, object]:
    configured_sha = os.getenv("KFCQUANT_SOURCE_SHA", "").strip()
    configured_lock_sha256 = os.getenv("KFCQUANT_DEPENDENCY_LOCK_SHA256", "").strip().lower()
    if configured_lock_sha256 and not re.fullmatch(r"[0-9a-f]{64}", configured_lock_sha256):
        raise RuntimeError("KFCQUANT_DEPENDENCY_LOCK_SHA256 must be a 64-character SHA-256")
    repository_root = Path(__file__).resolve().parents[2]
    source_sha = configured_sha
    source_dirty = False
    if not source_sha:
        try:
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            source_dirty = bool(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repository_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
        except (OSError, subprocess.SubprocessError):
            source_sha = "development"
            source_dirty = True
    lock_path = repository_root / "requirements.lock"
    dependency_lock_sha256 = configured_lock_sha256 or (
        hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.is_file() else "unavailable"
    )
    return {
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "dependency_lock_sha256": dependency_lock_sha256,
    }


def version_info(settings: Settings) -> dict[str, object]:
    return {
        "version": __version__,
        **build_identity(),
        "build_time": os.getenv("KFCQUANT_BUILD_TIME", "unknown"),
        "morning_strategy": settings.strategy_version_morning,
        "preclose_strategy": settings.strategy_version_preclose,
    }


def heartbeat_path(settings: Settings) -> Path:
    return settings.runtime_dir / "worker-heartbeat.json"


def write_heartbeat(settings: Settings) -> None:
    path = heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"at": datetime.now(SHANGHAI_TZ).isoformat(), **version_info(settings)}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def observe_worker_heartbeat(
    settings: Settings,
    observability: Observability | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    observability = observability or get_observability()
    observed_at = now or datetime.now(SHANGHAI_TZ)
    path = heartbeat_path(settings)
    if not path.exists():
        observability.alert(
            AlertCode.WORKER_HEARTBEAT_MISSING,
            "worker heartbeat file is missing",
            dedup_key=str(path),
            stage="worker.heartbeat",
        )
        return {"ok": False, "message": "heartbeat missing"}
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat_at = datetime.fromisoformat(str(heartbeat["at"]))
        if heartbeat_at.tzinfo is None:
            raise ValueError("worker heartbeat timestamp must include timezone")
        age_seconds = max(0.0, (observed_at - heartbeat_at).total_seconds())
        observability.metric(
            MetricName.WORKER_HEARTBEAT_AGE_SECONDS,
            age_seconds,
            unit="seconds",
            stage="worker.heartbeat",
        )
        heartbeat["age_seconds"] = age_seconds
        heartbeat["ok"] = age_seconds <= settings.worker_heartbeat_stale_seconds
        if not heartbeat["ok"]:
            observability.alert(
                AlertCode.WORKER_HEARTBEAT_STALE,
                "worker heartbeat exceeded the configured freshness threshold",
                dedup_key=str(path),
                stage="worker.heartbeat",
            )
        return heartbeat
    except Exception as exc:
        observability.alert(
            AlertCode.WORKER_HEARTBEAT_STALE,
            "worker heartbeat is unreadable",
            dedup_key=str(path),
            stage="worker.heartbeat",
        )
        return {"ok": False, "error": type(exc).__name__}


def health_info(settings: Settings, observability: Observability | None = None) -> dict[str, object]:
    observability = observability or get_observability()
    database = Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
        observability=observability,
    )
    result: dict[str, object] = {
        "status": "ok",
        "database": {"ok": False, "path": str(settings.database_path)},
        "worker": {"ok": False, "message": "heartbeat missing"},
        "disk": {},
        "providers": {
            "market": settings.market_provider,
            "live": settings.live_provider,
            "news": settings.news_provider,
            "llm": settings.llm_provider,
        },
        "version": version_info(settings),
    }
    try:
        if not settings.database_path.exists() and not settings.database_read_only:
            database.initialize()
        result["database"] = {"ok": True, "schema_version": database.migration_version()}
        result["latest_job"] = database.latest_job()
    except Exception as exc:
        result["status"] = "degraded"
        result["database"] = {"ok": False, "error": str(exc)}
    result["worker"] = observe_worker_heartbeat(settings, observability)
    if not result["worker"]["ok"]:
        result["status"] = "degraded"
    usage = shutil.disk_usage(settings.database_path.parent.resolve())
    result["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total, "ok": usage.free > 1_000_000_000}
    if not result["disk"]["ok"]:
        result["status"] = "degraded"
    return result
