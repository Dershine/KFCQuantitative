from __future__ import annotations

from datetime import date, datetime
from sys import version_info as PYTHON_VERSION_INFO
from typing import Any

from kfcquant.bootstrap import build_research_application
from kfcquant.clock import Clock
from kfcquant.config import Settings
from kfcquant.interfaces import LiveQuoteProvider, LLMProvider, MarketDataProvider, NewsProvider
from kfcquant.models import SignalRun
from kfcquant.observability import Observability
from kfcquant.strategy import StrategyRegistry
from kfcquant.unit_of_work import ResearchRunUnitOfWork


class Workflow:
    """Backward-compatible facade over independently testable application use cases."""

    def __init__(
        self,
        settings: Settings,
        database: Any | None = None,
        market_provider: MarketDataProvider | None = None,
        live_provider: LiveQuoteProvider | None = None,
        news_provider: NewsProvider | None = None,
        llm_provider: LLMProvider | None = None,
        run_uow: ResearchRunUnitOfWork | None = None,
        strategy_registry: StrategyRegistry | None = None,
        clock: Clock | None = None,
        observability: Observability | None = None,
    ):
        self._application = build_research_application(
            settings,
            database=database,
            market_provider=market_provider,
            live_provider=live_provider,
            news_provider=news_provider,
            llm_provider=llm_provider,
            run_uow=run_uow,
            strategy_registry=strategy_registry,
            clock=clock,
            python_version_info=PYTHON_VERSION_INFO,
            observability=observability,
        )
        for name in (
            "settings",
            "clock",
            "database",
            "repositories",
            "snapshot_store",
            "run_input_store",
            "point_in_time",
            "observability",
            "live_provider",
            "strategy_registry",
            "strategy_runner",
            "portfolio",
            "evaluation",
            "run_uow",
            "use_cases",
        ):
            setattr(self, name, getattr(self._application, name))

    @property
    def market_provider(self) -> MarketDataProvider:
        return self._application.market_provider

    @property
    def news_provider(self) -> NewsProvider:
        return self._application.news_provider

    @property
    def llm_provider(self) -> LLMProvider:
        return self._application.llm_provider

    def optional_llm(self) -> LLMProvider | None:
        return self._application.optional_llm()

    def doctor(self, online: bool = False) -> list[dict[str, object]]:
        return self.use_cases.doctor.execute(online)

    def sync_eod(self, start: date, end: date) -> dict[str, object]:
        return self.use_cases.sync_eod.execute(start, end)

    def sync_calendar(self, at: datetime | None = None) -> dict[str, object]:
        return self.use_cases.sync_calendar.execute(at)

    def run_preclose(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        return self.use_cases.run_preclose.execute(as_of, research_outside_window)

    def run_morning(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        return self.use_cases.run_morning.execute(as_of, research_outside_window)

    def evaluate_morning(self, at: datetime | None = None) -> list[object]:
        return self.use_cases.evaluate_morning.execute(at)

    def evaluate_previous_preclose(self, at: datetime | None = None) -> list[object]:
        return self.use_cases.evaluate_previous_preclose.execute(at)

    def capture_fill(self, at: datetime | None = None) -> list[object]:
        return self.use_cases.capture_fill.execute(at)

    def monitor_paper(self, at: datetime | None = None) -> list[object]:
        return self.use_cases.monitor_paper.execute(at)

    def run_postclose(self, at: datetime | None = None) -> str:
        return self.use_cases.run_postclose.execute(at)

    def recover_expired_jobs(self, at: datetime | None = None) -> list[str]:
        return self.use_cases.recover_expired_jobs.execute(at)
