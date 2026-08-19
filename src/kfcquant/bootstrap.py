from __future__ import annotations

from dataclasses import dataclass
from sys import version_info as PYTHON_VERSION_INFO

from kfcquant.application.queries import DashboardQueryModel
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
from kfcquant.models import SignalKind
from kfcquant.observability import Observability, get_observability, observe_provider
from kfcquant.point_in_time import PointInTimeDataGateway
from kfcquant.providers.document_loader import DocumentLoader
from kfcquant.providers.factory import (
    build_live_provider,
    build_llm_provider,
    build_market_provider,
    build_news_provider,
)
from kfcquant.query_models import DuckDBDashboardQueryModel
from kfcquant.repositories import DuckDBRepositories
from kfcquant.run_manifest import RunInputSnapshotStore
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.news import NewsService
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.reports import ReportService
from kfcquant.strategy import StrategyExecutionRunner, StrategyRegistry, build_default_strategy_registry
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork, ResearchRunUnitOfWork


class ProviderResolver:
    """Own lazy provider construction at the application composition boundary."""

    def __init__(
        self,
        settings: Settings,
        market_provider: MarketDataProvider | None,
        news_provider: NewsProvider | None,
        llm_provider: LLMProvider | None,
        observability: Observability,
    ):
        self.settings = settings
        self.observability = observability
        self._market_provider = market_provider
        self._news_provider = news_provider
        if (
            news_provider is None
            and market_provider is not None
            and hasattr(market_provider, "fetch_official_documents")
        ):
            self._news_provider = market_provider
        self._llm_provider = llm_provider
        self._observed: dict[str, object] = {}

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

    def observed_market_provider(self) -> MarketDataProvider:
        if "market" not in self._observed:
            self._observed["market"] = observe_provider(self.market_provider, self.observability)
        return self._observed["market"]  # type: ignore[return-value]

    def observed_news_provider(self) -> NewsProvider:
        if "news" not in self._observed:
            self._observed["news"] = observe_provider(self.news_provider, self.observability)
        return self._observed["news"]  # type: ignore[return-value]

    def observed_llm_provider(self) -> LLMProvider:
        if "llm" not in self._observed:
            self._observed["llm"] = observe_provider(self.llm_provider, self.observability)
        return self._observed["llm"]  # type: ignore[return-value]

    def optional_observed_llm(self) -> LLMProvider | None:
        try:
            return self.observed_llm_provider()
        except Exception:
            return None

    def optional_llm(self) -> LLMProvider | None:
        try:
            return self.llm_provider
        except Exception:
            return None


@dataclass(frozen=True)
class ResearchApplication:
    settings: Settings
    clock: Clock
    database: Database
    repositories: DuckDBRepositories
    snapshot_store: IngestionSnapshotStore
    run_input_store: RunInputSnapshotStore
    point_in_time: PointInTimeDataGateway
    observability: Observability
    providers: ProviderResolver
    live_provider: LiveQuoteProvider
    strategy_registry: StrategyRegistry
    strategy_runner: StrategyExecutionRunner
    portfolio: PortfolioService
    evaluation: CandidateEvaluationService
    run_uow: ResearchRunUnitOfWork
    use_cases: WorkflowUseCases

    @property
    def market_provider(self) -> MarketDataProvider:
        return self.providers.market_provider

    @property
    def news_provider(self) -> NewsProvider:
        return self.providers.news_provider

    @property
    def llm_provider(self) -> LLMProvider:
        return self.providers.llm_provider

    def optional_llm(self) -> LLMProvider | None:
        return self.providers.optional_llm()


def build_research_application(
    settings: Settings,
    database: Database | None = None,
    market_provider: MarketDataProvider | None = None,
    live_provider: LiveQuoteProvider | None = None,
    news_provider: NewsProvider | None = None,
    llm_provider: LLMProvider | None = None,
    run_uow: ResearchRunUnitOfWork | None = None,
    strategy_registry: StrategyRegistry | None = None,
    clock: Clock | None = None,
    python_version_info: tuple[int, ...] = PYTHON_VERSION_INFO,
    observability: Observability | None = None,
) -> ResearchApplication:
    """Build the Research Service object graph without leaking construction into use cases."""

    application_clock = clock or SystemClock(SHANGHAI_TZ)
    application_observability = observability or get_observability()
    application_database = database or Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
        observability=application_observability,
    )
    application_database.observability = application_observability
    application_database.initialize()
    repositories = DuckDBRepositories(application_database)
    snapshot_store = IngestionSnapshotStore(settings.raw_data_dir)
    run_input_store = RunInputSnapshotStore(settings.raw_data_dir)
    point_in_time = PointInTimeDataGateway(run_input_store, application_clock)
    providers = ProviderResolver(settings, market_provider, news_provider, llm_provider, application_observability)
    application_live_provider = live_provider or build_live_provider(settings)
    observed_live_provider = observe_provider(application_live_provider, application_observability)
    registry = strategy_registry or build_default_strategy_registry(settings)
    registry.require((SignalKind.MORNING_WATCHLIST, SignalKind.PRECLOSE_ENTRY))
    strategy_runner = StrategyExecutionRunner(registry)
    portfolio = PortfolioService(
        repositories.portfolio,
        settings,
        observed_live_provider,
        application_observability,
    )
    evaluation = CandidateEvaluationService(repositories.evaluation, settings, observed_live_provider)
    application_uow = run_uow or DuckDBResearchRunUnitOfWork(application_database, application_observability)

    jobs = JobController(repositories.jobs, settings, application_clock, application_observability)
    ingestor = MarketBatchIngestor(repositories.market, snapshot_store, application_clock)
    manifests = RunManifestFactory(application_clock)
    news = NewsSynchronizer(
        lambda: NewsService(
            repositories.news,
            providers.observed_news_provider(),
            providers.optional_observed_llm(),
            DocumentLoader(settings.max_document_bytes),
            application_observability,
            settings.official_news_backlog_threshold,
        )
    )
    evaluate_morning = EvaluateMorningUseCase(repositories.research, evaluation, jobs, application_clock)
    evaluate_previous = EvaluatePreviousPrecloseUseCase(repositories.research, evaluation, application_clock)
    use_cases = WorkflowUseCases(
        doctor=DoctorUseCase(
            settings,
            application_clock,
            lambda: providers.observed_market_provider(),
            observed_live_provider,
            lambda: providers.observed_news_provider(),
            lambda: providers.observed_llm_provider(),
            python_version_info,
        ),
        sync_eod=SyncEodUseCase(lambda: providers.observed_market_provider(), ingestor, jobs, application_clock),
        sync_calendar=SyncCalendarUseCase(
            repositories.market,
            lambda: providers.observed_market_provider(),
            ingestor,
            jobs,
            application_clock,
        ),
        run_preclose=RunPrecloseUseCase(
            settings,
            repositories.research,
            observed_live_provider,
            news,
            ingestor,
            point_in_time,
            registry,
            strategy_runner,
            portfolio,
            application_uow,
            manifests,
            jobs,
            application_clock,
            application_observability,
        ),
        run_morning=RunMorningUseCase(
            settings,
            repositories.research,
            news,
            point_in_time,
            registry,
            strategy_runner,
            application_uow,
            manifests,
            jobs,
            application_clock,
            application_observability,
        ),
        evaluate_morning=evaluate_morning,
        evaluate_previous_preclose=evaluate_previous,
        capture_fill=CaptureFillUseCase(
            settings,
            repositories.research,
            observed_live_provider,
            ingestor,
            portfolio,
            jobs,
            application_clock,
        ),
        monitor_paper=MonitorPaperUseCase(
            settings,
            repositories.research,
            portfolio,
            jobs,
            application_clock,
        ),
        run_postclose=RunPostcloseUseCase(
            settings,
            repositories.research,
            news,
            evaluate_morning,
            evaluate_previous,
            lambda: ReportService(
                repositories.report,
                providers.optional_observed_llm(),
                settings.report_dir,
                settings.llm_report_model,
            ),
            jobs,
            application_clock,
        ),
        recover_expired_jobs=RecoverExpiredJobsUseCase(repositories.jobs, application_clock),
    )
    return ResearchApplication(
        settings=settings,
        clock=application_clock,
        database=application_database,
        repositories=repositories,
        snapshot_store=snapshot_store,
        run_input_store=run_input_store,
        point_in_time=point_in_time,
        observability=application_observability,
        providers=providers,
        live_provider=application_live_provider,
        strategy_registry=registry,
        strategy_runner=strategy_runner,
        portfolio=portfolio,
        evaluation=evaluation,
        run_uow=application_uow,
        use_cases=use_cases,
    )


def build_dashboard_query_model(
    settings: Settings,
    database: Database | None = None,
) -> DashboardQueryModel:
    """Build the Dashboard's read-only application boundary."""

    query_database = database or Database(
        settings.database_path,
        settings.initial_cash,
        settings.database_lock_timeout_seconds,
        settings.runtime_dir / "database.lock",
    )
    if not settings.database_read_only:
        query_database.initialize()
    return DuckDBDashboardQueryModel(query_database)
