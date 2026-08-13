from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class Settings(BaseSettings):
    """Runtime settings. Secrets are loaded from .env and never persisted."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KFCQUANT_",
        extra="ignore",
    )

    database_path: Path = Path("data/kfcquant.duckdb")
    raw_data_dir: Path = Path("data/raw")
    report_dir: Path = Path("reports")
    runtime_dir: Path = Path("runtime")
    backup_dir: Path = Path("backups")
    database_lock_timeout_seconds: int = 30
    database_read_only: bool = False
    base_url_path: str = ""

    data_profile: str = "learning"
    market_provider: str = "baostock"
    live_provider: str = "akshare"
    news_provider: str = "akshare"
    llm_provider: str = "deepseek"

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

    initial_cash: float = 100_000.0
    max_positions: int = 5
    position_fraction: float = 0.20
    lot_size: int = 100
    take_profit_net: float = 0.015
    stop_loss_net: float = 0.02
    max_holding_days: int = 5
    score_exit_threshold: float = 60.0

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0005

    min_listing_trading_days: int = 120
    min_median_amount_20d: float = 100_000_000.0
    quote_freshness_seconds: int = 60
    limit_distance_fraction: float = 0.01
    preclose_hour: int = 14
    preclose_minute: int = 40
    fill_hour: int = 14
    fill_minute: int = 45
    morning_hour: int = 8
    morning_minute: int = 30
    strategy_version_morning: str = "morning-v1"
    strategy_version_preclose: str = "preclose-v2"

    news_lookback_trading_days: int = 5
    news_sources: tuple[str, ...] = ("cls", "yicai", "sina")
    max_document_bytes: int = 12_000_000

    @property
    def dashscope_api_key(self) -> str | None:
        """Backward-compatible name used by older local .env files."""
        return self.llm_api_key

    @property
    def dashscope_base_url(self) -> str:
        return self.llm_base_url

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
