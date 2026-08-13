from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".ops.env", env_prefix="KFCOPS_", extra="ignore")

    database_path: Path = Path("/var/lib/kfcops/ops.sqlite3")
    compose_directory: Path = Path("/opt/kfcquant/research")
    compose_file: Path = Path("/opt/kfcquant/research/compose.yaml")
    release_env_file: Path = Path("/opt/kfcquant/research/.release.env")
    research_database: Path = Path("/var/lib/kfcquant/data/kfcquant.duckdb")
    research_lock: Path = Path("/var/lib/kfcquant/runtime/database.lock")
    certificate_path: Path | None = None
    backup_directory: Path = Path("/var/lib/kfcquant/backups")
    github_repository: str = "Dershine/KFCQuantitative"
    github_token: str = ""
    session_secret: str = "change-me"
    research_health_url: str = "http://127.0.0.1:8501/research/_stcore/health"
    timezone: str = "Asia/Shanghai"
    backup_retention: int = 7
