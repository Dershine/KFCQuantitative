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
    repository_directory: Path = Path("/opt/kfcquant/repository")
    releases_directory: Path = Path("/opt/kfcquant/releases")
    current_release_link: Path = Path("/opt/kfcquant/current")
    builder_python: Path = Path("/usr/bin/python3")
    service_control_command: Path = Path("/usr/local/sbin/kfcquant-service-control")
    research_database: Path = Path("/var/lib/kfcquant/data/kfcquant.duckdb")
    research_lock: Path = Path("/var/lib/kfcquant/runtime/database.lock")
    certificate_path: Path | None = None
    backup_directory: Path = Path("/var/lib/kfcquant/backups")
    assurance_directory: Path = Path("/var/lib/kfcops/assurance")
    metrics_path: Path = Path("/var/lib/kfcquant/runtime/observability-metrics.jsonl")
    raw_data_directory: Path = Path("/var/lib/kfcquant/data/raw")
    github_repository: str = "Dershine/KFCQuantitative"
    github_token: str = ""
    session_secret: str = "change-me"
    research_health_url: str = "http://127.0.0.1:8501/research/_stcore/health"
    timezone: str = "Asia/Shanghai"
    backup_retention: int = Field(default=7, ge=1, le=365)
    capacity_minimum_job_samples: int = Field(default=20, ge=1, le=10_000)
    capacity_minimum_lock_samples: int = Field(default=100, ge=1, le=100_000)
    capacity_minimum_query_samples: int = Field(default=20, ge=1, le=1_000)
    capacity_signal_runtime_budget_seconds: float = Field(default=384.0, gt=0)
    capacity_lock_wait_p95_limit_seconds: float = Field(default=1.0, ge=0)
    capacity_query_p95_limit_seconds: float = Field(default=2.0, gt=0)
    capacity_recovery_rto_seconds: float = Field(default=300.0, gt=0)
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
        repository = self.repository_directory.absolute()
        releases = self.releases_directory.absolute()
        current = self.current_release_link.absolute()
        if len({repository, releases, current}) != 3:
            raise ValueError("repository_directory, releases_directory, and current_release_link must be distinct")
        pairs = ((repository, releases), (repository, current), (releases, current))
        if any(left in right.parents or right in left.parents for left, right in pairs):
            raise ValueError("repository, releases, and current link paths must not be nested unsafely")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        return self
