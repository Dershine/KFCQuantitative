from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".ops.env", env_prefix="KFCOPS_", extra="ignore")

    database_path: Path = Path("/var/lib/kfcops/ops.sqlite3")
    deployment_lock: Path = Path("/var/lib/kfcops/deploy.lock")
    repository_directory: Path = Path("/opt/kfcquant/app")
    virtualenv_directory: Path = Path("/opt/kfcquant/app/.venv")
    release_env_file: Path = Path("/opt/kfcquant/app/.release.env")
    service_control_command: Path = Path("/usr/local/sbin/kfcquant-service-control")
    research_database: Path = Path("/var/lib/kfcquant/data/kfcquant.duckdb")
    research_lock: Path = Path("/var/lib/kfcquant/runtime/database.lock")
    certificate_path: Path | None = None
    backup_directory: Path = Path("/var/lib/kfcquant/backups")
    github_repository: str = "Dershine/KFCQuantitative"
    github_token: str = ""
    session_secret: str = "change-me"
    research_health_url: str = "http://127.0.0.1:8501/research/_stcore/health"
    timezone: str = "Asia/Shanghai"
    backup_retention: int = Field(default=7, ge=1, le=365)
    protected_window_start: time = time(8, 15)
    protected_window_end: time = time(15, 10)

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> OpsSettings:
        if self.session_secret == "change-me" or len(self.session_secret.strip()) < 32:
            raise ValueError("session_secret must be configured with at least 32 characters")
        if self.protected_window_start >= self.protected_window_end:
            raise ValueError("protected window start must be before protected window end")
        if self.research_health_url.startswith(("http://", "https://")) is False:
            raise ValueError("research_health_url must use http or https")
        if self.github_repository.count("/") != 1 or any(not part for part in self.github_repository.split("/")):
            raise ValueError("github_repository must use owner/repository format")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        return self
