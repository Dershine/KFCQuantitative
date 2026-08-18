from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from kfcquant import __version__
from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.db import Database


@lru_cache(maxsize=1)
def build_identity() -> dict[str, object]:
    configured_sha = os.getenv("KFCQUANT_SOURCE_SHA", "").strip()
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
    dependency_lock_sha256 = (
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


def health_info(settings: Settings) -> dict[str, object]:
    database = Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
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
    path = heartbeat_path(settings)
    if path.exists():
        try:
            heartbeat = json.loads(path.read_text(encoding="utf-8"))
            heartbeat_at = datetime.fromisoformat(str(heartbeat["at"]))
            heartbeat["ok"] = datetime.now(SHANGHAI_TZ) - heartbeat_at <= timedelta(minutes=3)
            result["worker"] = heartbeat
            if not heartbeat["ok"]:
                result["status"] = "degraded"
        except Exception as exc:
            result["worker"] = {"ok": False, "error": str(exc)}
            result["status"] = "degraded"
    usage = shutil.disk_usage(settings.database_path.parent.resolve())
    result["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total, "ok": usage.free > 1_000_000_000}
    if not result["disk"]["ok"]:
        result["status"] = "degraded"
    return result
