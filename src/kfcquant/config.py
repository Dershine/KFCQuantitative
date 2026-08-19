from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kfcquant.policies import SchedulePolicy, SelectionPolicy

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
StrategyVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class Settings(BaseSettings):
    """Runtime settings. Secrets are loaded from .env and never persisted."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KFCQUANT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    database_path: Path = Path("data/kfcquant.duckdb")
    raw_data_dir: Path = Path("data/raw")
    report_dir: Path = Path("reports")
    runtime_dir: Path = Path("runtime")
    backup_dir: Path = Path("backups")
    database_lock_timeout_seconds: int = Field(default=30, ge=0, le=3600)
    job_lease_seconds: int = Field(default=900, ge=60, le=86_400)
    database_read_only: bool = False
    base_url_path: str = ""
    alert_webhook_url: str | None = None
    alert_webhook_bearer_token: str | None = None
    alert_cooldown_seconds: int = Field(default=900, ge=0, le=86_400)
    worker_heartbeat_stale_seconds: int = Field(default=180, ge=60, le=3_600)
    official_news_backlog_threshold: int = Field(default=100, ge=1, le=100_000)

    data_profile: Literal["learning"] = "learning"
    market_provider: Literal["baostock", "tushare"] = "baostock"
    live_provider: Literal["akshare"] = "akshare"
    news_provider: Literal["akshare", "tushare"] = "akshare"
    llm_provider: Literal["deepseek", "qwen", "openai-compatible"] = "deepseek"

    tushare_token: str | None = Field(default=None, validation_alias="TUSHARE_TOKEN")
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("KFCQUANT_LLM_BASE_URL", "KFCQUANT_DASHSCOPE_BASE_URL"),
    )
    llm_extract_model: str = "deepseek-v4-flash"
    llm_report_model: str = "deepseek-v4-pro"

    initial_cash: float = Field(default=100_000.0, gt=0)
    max_positions: int = Field(default=5, ge=1, le=100)
    position_fraction: float = Field(default=0.20, gt=0, le=1)
    lot_size: int = Field(default=100, ge=1)
    take_profit_net: float = Field(default=0.015, gt=0, lt=1)
    stop_loss_net: float = Field(default=0.02, gt=0, lt=1)
    max_holding_days: int = Field(default=5, ge=1)
    score_exit_threshold: float = Field(default=60.0, ge=0, le=100)

    commission_rate: float = Field(default=0.00025, ge=0, lt=0.1)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_duty_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)

    min_listing_trading_days: int = Field(default=120, ge=1)
    min_median_amount_20d: float = Field(default=100_000_000.0, gt=0)
    quote_freshness_seconds: int = Field(default=60, ge=1, le=3600)
    limit_distance_fraction: float = Field(default=0.01, ge=0, lt=0.1)
    strategy_version_morning: StrategyVersion = "morning-v1"
    strategy_version_preclose: StrategyVersion = "preclose-v2"
    schedule: SchedulePolicy = Field(default_factory=SchedulePolicy)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)

    news_lookback_trading_days: int = Field(default=5, ge=1)
    news_sources: tuple[str, ...] = ("cls", "yicai", "sina")
    max_document_bytes: int = Field(default=12_000_000, ge=1)

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> Settings:
        if self.max_positions * self.position_fraction > 1.0 + 1e-12:
            raise ValueError("max_positions * position_fraction must not exceed the initial paper account")
        if self.selection.top_n < self.max_positions:
            raise ValueError("selection.top_n must be at least max_positions so reserve selection cannot underfill")
        if "tushare" in {self.market_provider, self.news_provider} and not self.tushare_token:
            raise ValueError("TUSHARE_TOKEN is required when a Tushare provider is selected")
        if not self.news_sources:
            raise ValueError("news_sources must not be empty")
        if self.alert_webhook_url and not self.alert_webhook_url.startswith(("https://", "http://")):
            raise ValueError("alert_webhook_url must use http or https")
        if self.alert_webhook_bearer_token and not self.alert_webhook_url:
            raise ValueError("alert_webhook_url is required when alert_webhook_bearer_token is configured")
        return self

    @property
    def dashscope_api_key(self) -> str | None:
        """Backward-compatible name used by older local .env files."""
        return self.llm_api_key

    @property
    def dashscope_base_url(self) -> str:
        return self.llm_base_url

    @property
    def metrics_path(self) -> Path:
        return self.runtime_dir / "observability-metrics.jsonl"

    @property
    def alerts_path(self) -> Path:
        return self.runtime_dir / "observability-alerts.jsonl"

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_data_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
