from __future__ import annotations

from datetime import date, datetime
from sys import version_info as PYTHON_VERSION_INFO

from kfcquant.application.use_cases import (
    CaptureFillUseCase,
    DoctorUseCase,
    EvaluateMorningUseCase,
    EvaluatePreviousPrecloseUseCase,
    JobController,
    MarketBatchIngestor,
    MonitorPaperUseCase,
    NewsSynchronizer,
    RecoverExpiredJobsUseCase,
    RunManifestFactory,
    RunMorningUseCase,
    RunPostcloseUseCase,
    RunPrecloseUseCase,
    SyncCalendarUseCase,
    SyncEodUseCase,
    WorkflowUseCases,
)
from kfcquant.clock import Clock, SystemClock
from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.db import Database
from kfcquant.ingestion import IngestionSnapshotStore
from kfcquant.interfaces import LiveQuoteProvider, LLMProvider, MarketDataProvider, NewsProvider
from kfcquant.models import SignalKind, SignalRun
from kfcquant.point_in_time import PointInTimeDataGateway
from kfcquant.providers.document_loader import DocumentLoader
from kfcquant.providers.factory import (
    build_live_provider,
    build_llm_provider,
    build_market_provider,
    build_news_provider,
)
from kfcquant.repositories import DuckDBRepositories
from kfcquant.run_manifest import RunInputSnapshotStore
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.news import NewsService
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.reports import ReportService
from kfcquant.strategy import StrategyExecutionRunner, StrategyRegistry, build_default_strategy_registry
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork, ResearchRunUnitOfWork


class Workflow:
    """Backward-compatible facade over independently testable application use cases."""

    def __init__(
        self,
        settings: Settings,
        database: Database | None = None,
        market_provider: MarketDataProvider | None = None,
        live_provider: LiveQuoteProvider | None = None,
        news_provider: NewsProvider | None = None,
        llm_provider: LLMProvider | None = None,
        run_uow: ResearchRunUnitOfWork | None = None,
        strategy_registry: StrategyRegistry | None = None,
        clock: Clock | None = None,
    ):
        self.settings = settings
        self.clock = clock or SystemClock(SHANGHAI_TZ)
        self.database = database or Database(
            settings.database_path,
            settings.initial_cash,
            settings.database_lock_timeout_seconds,
            settings.runtime_dir / "database.lock",
        )
        self.database.initialize()
        self.repositories = DuckDBRepositories(self.database)
        self.snapshot_store = IngestionSnapshotStore(settings.raw_data_dir)
        self.run_input_store = RunInputSnapshotStore(settings.raw_data_dir)
        self.point_in_time = PointInTimeDataGateway(self.run_input_store, self.clock)
        self.live_provider = live_provider or build_live_provider(settings)
        self._market_provider = market_provider
        self._news_provider = news_provider
        if (
            news_provider is None
            and market_provider is not None
            and hasattr(market_provider, "fetch_official_documents")
        ):
            self._news_provider = market_provider
        self._llm_provider = llm_provider
        self.strategy_registry = strategy_registry or build_default_strategy_registry(settings)
        self.strategy_registry.require((SignalKind.MORNING_WATCHLIST, SignalKind.PRECLOSE_ENTRY))
        self.strategy_runner = StrategyExecutionRunner(self.strategy_registry)
        self.portfolio = PortfolioService(self.repositories.portfolio, settings, self.live_provider)
        self.evaluation = CandidateEvaluationService(self.repositories.evaluation, settings, self.live_provider)
        self.run_uow = run_uow or DuckDBResearchRunUnitOfWork(self.database)
        self.use_cases = self._build_use_cases()

    @property
    def market_provider(self) -> MarketDataProvider:
        if self._market_provider is None:
            self._market_provider = build_market_provider(self.settings)
        return self._market_provider

    @property
    def news_provider(self) -> NewsProvider:
        if self._news_provider is None:
            self._news_provider = build_news_provider(self.settings)
        return self._news_provider

    @property
    def llm_provider(self) -> LLMProvider:
        if self._llm_provider is None:
            self._llm_provider = build_llm_provider(self.settings)
        return self._llm_provider

    def optional_llm(self) -> LLMProvider | None:
        try:
            return self.llm_provider
        except Exception:
            return None

    def _build_use_cases(self) -> WorkflowUseCases:
        jobs = JobController(self.repositories.jobs, self.settings, self.clock)
        ingestor = MarketBatchIngestor(self.repositories.market, self.snapshot_store, self.clock)
        manifests = RunManifestFactory(self.clock)
        news = NewsSynchronizer(
            lambda: NewsService(
                self.repositories.news,
                self.news_provider,
                self.optional_llm(),
                DocumentLoader(self.settings.max_document_bytes),
            )
        )
        evaluate_morning = EvaluateMorningUseCase(self.repositories.research, self.evaluation, jobs, self.clock)
        evaluate_previous = EvaluatePreviousPrecloseUseCase(
            self.repositories.research,
            self.evaluation,
            self.clock,
        )
        return WorkflowUseCases(
            doctor=DoctorUseCase(
                self.settings,
                self.clock,
                lambda: self.market_provider,
                self.live_provider,
                lambda: self.news_provider,
                lambda: self.llm_provider,
                PYTHON_VERSION_INFO,
            ),
            sync_eod=SyncEodUseCase(lambda: self.market_provider, ingestor, jobs, self.clock),
            sync_calendar=SyncCalendarUseCase(
                self.repositories.market,
                lambda: self.market_provider,
                ingestor,
                jobs,
                self.clock,
            ),
            run_preclose=RunPrecloseUseCase(
                self.settings,
                self.repositories.research,
                self.live_provider,
                news,
                ingestor,
                self.point_in_time,
                self.strategy_registry,
                self.strategy_runner,
                self.portfolio,
                self.run_uow,
                manifests,
                jobs,
                self.clock,
            ),
            run_morning=RunMorningUseCase(
                self.settings,
                self.repositories.research,
                news,
                self.point_in_time,
                self.strategy_registry,
                self.strategy_runner,
                self.run_uow,
                manifests,
                jobs,
                self.clock,
            ),
            evaluate_morning=evaluate_morning,
            evaluate_previous_preclose=evaluate_previous,
            capture_fill=CaptureFillUseCase(
                self.settings,
                self.repositories.research,
                self.live_provider,
                ingestor,
                self.portfolio,
                jobs,
                self.clock,
            ),
            monitor_paper=MonitorPaperUseCase(
                self.settings,
                self.repositories.research,
                self.portfolio,
                jobs,
                self.clock,
            ),
            run_postclose=RunPostcloseUseCase(
                self.settings,
                self.repositories.research,
                news,
                evaluate_morning,
                evaluate_previous,
                lambda: ReportService(
                    self.repositories.report,
                    self.optional_llm(),
                    self.settings.report_dir,
                    self.settings.llm_report_model,
                ),
                jobs,
                self.clock,
            ),
            recover_expired_jobs=RecoverExpiredJobsUseCase(self.repositories.jobs, self.clock),
        )

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
