import platform
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from sys import version as PYTHON_VERSION
from sys import version_info as PYTHON_VERSION_INFO
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pandas as pd

from kfcquant import __version__
from kfcquant.application.errors import JobLeaseLostError
from kfcquant.application.ports import JobRepository, MarketRepository, ResearchRepository
from kfcquant.clock import Clock
from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.ingestion import IngestionManifest, IngestionSnapshotStore, resolve_provider_identity
from kfcquant.interfaces import LiveQuoteProvider, LLMProvider, MarketDataProvider, NewsProvider
from kfcquant.market_data import (
    DAILY_BAR_SCHEMA,
    LIVE_QUOTE_SCHEMA,
    SECURITY_SCHEMA,
    TRADE_CALENDAR_SCHEMA,
    MarketTableSchema,
)
from kfcquant.models import ResearchRunState, RunStatus, SignalKind, SignalRun
from kfcquant.observability import AlertCode, MetricName, Observability, get_observability
from kfcquant.point_in_time import PointInTimeDataGateway
from kfcquant.run_manifest import ResearchRunManifest, RunInputSnapshot
from kfcquant.runtime import build_identity
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.news import NewsService, NewsSyncResult
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.reports import ReportService
from kfcquant.strategy import StrategyExecutionRunner, StrategyRegistry
from kfcquant.unit_of_work import JobCompletion, ResearchRunUnitOfWork

MINIMUM_PYTHON_VERSION = (3, 12)


def validated_market_frame(schema: MarketTableSchema, frame: pd.DataFrame) -> pd.DataFrame:
    """Recheck injected and production providers at the application trust boundary."""
    return schema.validate(frame).frame


def _lease_heartbeat_interval_seconds(settings: Settings) -> float:
    return max(1.0, min(30.0, settings.job_lease_seconds / 3))


class JobController:
    def __init__(
        self,
        repository: JobRepository,
        settings: Settings,
        clock: Clock,
        observability: Observability | None = None,
    ):
        self.repository = repository
        self.settings = settings
        self.clock = clock
        self.observability = observability or get_observability()
        self._contexts: dict[str, Any] = {}
        self._jobs: dict[str, tuple[str, datetime, datetime | None]] = {}

    def start(self, name: str, at: datetime, scheduled_for: datetime | None = None) -> str:
        job_id = str(uuid4())
        self.repository.start_job(
            job_id,
            name,
            at,
            timedelta(seconds=self.settings.job_lease_seconds),
            scheduled_for=scheduled_for,
        )
        self._jobs[job_id] = (name, at, scheduled_for)
        self._contexts[job_id] = self.observability.begin_context(job_run_id=job_id, stage=name)
        self.observability.event("job_started", job_name=name, started_at=at)
        return job_id

    def heartbeat(self, job_id: str) -> datetime:
        heartbeat_at = self.clock.now()
        renewed = self.repository.heartbeat_job(
            job_id,
            heartbeat_at,
            timedelta(seconds=self.settings.job_lease_seconds),
        )
        if not renewed:
            raise JobLeaseLostError(f"job lease is no longer active: {job_id}")
        return heartbeat_at

    def finish(self, job_id: str, status: str, message: str, **metadata: Any) -> None:
        finished_at = self.heartbeat(job_id)
        self.repository.finish_job(job_id, finished_at, status, message, metadata)
        job_name, started, _ = self._jobs.get(job_id, ("unknown", finished_at, None))
        self._record_completion(job_id, job_name, started, finished_at, status, message, metadata)

    def completion(
        self,
        job_id: str,
        name: str,
        started: datetime,
        status: str,
        message: str,
        **metadata: Any,
    ) -> JobCompletion:
        finished_at = self.heartbeat(job_id)
        completion = JobCompletion(
            job_run_id=job_id,
            job_name=name,
            started_at=started,
            finished_at=finished_at,
            status=status,
            message=message,
            scheduled_for=self._jobs.get(job_id, (name, started, None))[2],
            metadata=metadata,
        )
        self._close_context(job_id)
        return completion

    def _record_completion(
        self,
        job_id: str,
        name: str,
        started: datetime,
        finished_at: datetime,
        status: str,
        message: str,
        metadata: dict[str, Any],
    ) -> None:
        duration = max(0.0, (finished_at - started).total_seconds())
        self.observability.metric(
            MetricName.JOB_DURATION_SECONDS,
            duration,
            unit="seconds",
            labels={"job_name": name, "status": status},
        )
        status_metrics = {
            "success": MetricName.JOB_SUCCESS_TOTAL,
            "failed": MetricName.JOB_FAILED_TOTAL,
            "missed": MetricName.JOB_MISSED_TOTAL,
        }
        if metric := status_metrics.get(status):
            self.observability.metric(metric, 1, labels={"job_name": name})
        self.observability.event(
            "job_finished",
            severity="error" if status == "failed" else "info",
            job_name=name,
            status=status,
            duration_seconds=duration,
            message=message,
            metadata=metadata,
        )
        if (
            name == "run-preclose"
            and status == "failed"
            and self.settings.schedule.preclose_window.contains(started)
        ):
            self.observability.alert(
                AlertCode.PRECLOSE_RUN_FAILED,
                "scheduled pre-close Signal Run failed",
                dedup_key=started.date().isoformat(),
            )
        self._close_context(job_id)

    def _close_context(self, job_id: str) -> None:
        token = self._contexts.pop(job_id, None)
        self._jobs.pop(job_id, None)
        if token is not None:
            self.observability.end_context(token)


class MarketBatchIngestor:
    def __init__(
        self,
        repository: MarketRepository,
        snapshot_store: IngestionSnapshotStore,
        clock: Clock,
    ):
        self.repository = repository
        self.snapshot_store = snapshot_store
        self.clock = clock

    def ingest(
        self,
        schema: MarketTableSchema,
        frame: pd.DataFrame,
        provider: object,
        job_run_id: str,
    ) -> IngestionManifest:
        validated = schema.validate(frame)
        manifest = self.snapshot_store.capture(
            validated,
            resolve_provider_identity(provider, validated),
            self.clock.now(),
            job_run_id,
        )
        self.repository.ingest_market_batch(validated.frame, manifest)
        return manifest


class NewsSynchronizer:
    def __init__(
        self,
        service_factory: Callable[[], NewsService],
    ):
        self.service_factory = service_factory

    def sync(
        self,
        start: datetime,
        end: datetime,
        *,
        heartbeat: Callable[[], object] | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ) -> NewsSyncResult:
        try:
            service = self.service_factory()
        except Exception as exc:
            return NewsSyncResult(False, False, 0, 0, 0, 0, [str(exc)])
        if heartbeat is None:
            return service.sync(start, end)
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")

        stopped = Event()
        failures: list[Exception] = []

        def renew_lease() -> None:
            while not stopped.wait(heartbeat_interval_seconds):
                try:
                    heartbeat()
                except Exception as exc:
                    failures.append(exc)
                    return

        heartbeat()
        thread = Thread(target=renew_lease, name="news-job-lease-heartbeat", daemon=True)
        thread.start()
        try:
            result = service.sync(start, end)
        finally:
            stopped.set()
            thread.join()
        if failures:
            raise failures[0]
        return result


class RunManifestFactory:
    def __init__(self, clock: Clock):
        self.clock = clock

    def create(
        self,
        run: SignalRun,
        snapshots: tuple[RunInputSnapshot, ...],
        result_sha256: str,
    ) -> ResearchRunManifest:
        identity = build_identity()
        return ResearchRunManifest.create(
            run,
            snapshots,
            result_sha256,
            source_sha=str(identity["source_sha"]),
            source_dirty=bool(identity["source_dirty"]),
            project_version=__version__,
            python_version=platform.python_version(),
            dependency_lock_sha256=str(identity["dependency_lock_sha256"]),
            created_at=self.clock.now(),
        )


class DoctorUseCase:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        market_provider: Callable[[], MarketDataProvider],
        live_provider: LiveQuoteProvider,
        news_provider: Callable[[], NewsProvider],
        llm_provider: Callable[[], LLMProvider],
        python_version_info: tuple[int, ...] = PYTHON_VERSION_INFO,
    ):
        self.settings = settings
        self.clock = clock
        self.market_provider = market_provider
        self.live_provider = live_provider
        self.news_provider = news_provider
        self.llm_provider = llm_provider
        self.python_version_info = python_version_info

    def execute(self, online: bool = False) -> list[dict[str, object]]:
        checks: list[dict[str, object]] = [
            {
                "check": "python",
                "ok": self.python_version_info >= MINIMUM_PYTHON_VERSION,
                "detail": PYTHON_VERSION.split()[0],
            },
            {
                "check": "database",
                "ok": self.settings.database_path.parent.exists(),
                "detail": str(self.settings.database_path),
            },
            {"check": "data-profile", "ok": True, "detail": self.settings.data_profile},
            {
                "check": "market-provider",
                "ok": self.settings.market_provider.lower() in {"baostock", "tushare"},
                "detail": self.settings.market_provider,
            },
            {
                "check": "news-provider",
                "ok": self.settings.news_provider.lower() in {"akshare", "tushare"},
                "detail": self.settings.news_provider,
            },
        ]
        if "tushare" in {self.settings.market_provider.lower(), self.settings.news_provider.lower()}:
            checks.append(
                {
                    "check": "TUSHARE_TOKEN",
                    "ok": bool(self.settings.tushare_token),
                    "detail": "configured" if self.settings.tushare_token else "missing",
                }
            )
        checks.append(
            {
                "check": "LLM_API_KEY",
                "ok": bool(self.settings.llm_api_key),
                "detail": "configured" if self.settings.llm_api_key else "missing",
            }
        )
        modules = {"duckdb", "pandas", "streamlit", "openai", "pyarrow"}
        if self.settings.market_provider.lower() == "baostock":
            modules.add("baostock")
        if self.settings.market_provider.lower() == "tushare" or self.settings.news_provider.lower() == "tushare":
            modules.add("tushare")
        if self.settings.live_provider.lower() == "akshare" or self.settings.news_provider.lower() == "akshare":
            modules.add("akshare")
        for module in sorted(modules):
            try:
                __import__(module)
                checks.append({"check": f"module:{module}", "ok": True, "detail": "available"})
            except Exception as exc:
                checks.append({"check": f"module:{module}", "ok": False, "detail": str(exc)})
        if not online:
            return checks
        try:
            today = self.clock.now().date()
            frame = validated_market_frame(
                TRADE_CALENDAR_SCHEMA,
                self.market_provider().fetch_trade_calendar(today - timedelta(days=2), today),
            )
            checks.append(
                {
                    "check": f"{self.settings.market_provider}-online",
                    "ok": not frame.empty,
                    "detail": f"{len(frame)} calendar rows",
                }
            )
        except Exception as exc:
            checks.append({"check": f"{self.settings.market_provider}-online", "ok": False, "detail": str(exc)})
        try:
            frame = validated_market_frame(LIVE_QUOTE_SCHEMA, self.live_provider.fetch_quotes())
            checks.append({"check": "akshare-online", "ok": not frame.empty, "detail": f"{len(frame)} quotes"})
        except Exception as exc:
            checks.append({"check": "akshare-online", "ok": False, "detail": str(exc)})
        try:
            now = self.clock.now()
            documents = self.news_provider().fetch_official_documents(now - timedelta(days=1), now)
            checks.append(
                {
                    "check": f"{self.settings.news_provider}-announcements-online",
                    "ok": True,
                    "detail": f"reachable; {len(documents)} dated documents",
                }
            )
        except Exception as exc:
            checks.append(
                {"check": f"{self.settings.news_provider}-announcements-online", "ok": False, "detail": str(exc)}
            )
        if self.settings.llm_api_key:
            try:
                provider = self.llm_provider()
                healthcheck = getattr(provider, "healthcheck", None)
                detail = healthcheck() if healthcheck else "provider created"
                checks.append({"check": f"{self.settings.llm_provider}-online", "ok": True, "detail": detail})
            except Exception as exc:
                checks.append({"check": f"{self.settings.llm_provider}-online", "ok": False, "detail": str(exc)})
        return checks


class SyncEodUseCase:
    def __init__(
        self,
        market_provider: Callable[[], MarketDataProvider],
        ingestor: MarketBatchIngestor,
        jobs: JobController,
        clock: Clock,
    ):
        self.market_provider = market_provider
        self.ingestor = ingestor
        self.jobs = jobs
        self.clock = clock

    def execute(self, start: date, end: date) -> dict[str, object]:
        started = self.clock.now()
        job_id = self.jobs.start("sync-eod", started)
        try:
            provider = self.market_provider()
            securities = validated_market_frame(SECURITY_SCHEMA, provider.fetch_securities())
            self.jobs.heartbeat(job_id)
            manifests = [self.ingestor.ingest(SECURITY_SCHEMA, securities, provider, job_id)]
            calendar = validated_market_frame(TRADE_CALENDAR_SCHEMA, provider.fetch_trade_calendar(start, end))
            self.jobs.heartbeat(job_id)
            manifests.append(self.ingestor.ingest(TRADE_CALENDAR_SCHEMA, calendar, provider, job_id))
            open_dates = [row["cal_date"] for row in calendar.to_dict("records") if bool(row["is_open"])]
            rows = 0
            range_loader = getattr(provider, "iter_daily_range", None)
            if range_loader:
                list_dates = pd.to_datetime(securities["list_date"], errors="coerce")
                delist_dates = pd.to_datetime(securities["delist_date"], errors="coerce")
                eligible = securities[
                    (list_dates <= pd.Timestamp(end)) & (delist_dates.isna() | (delist_dates >= pd.Timestamp(start)))
                ]
                frames = range_loader(start, end, eligible["ts_code"].astype(str).tolist())
            else:
                frames = (provider.fetch_daily(trade_date) for trade_date in open_dates)
            for frame in frames:
                frame = validated_market_frame(DAILY_BAR_SCHEMA, frame)
                self.jobs.heartbeat(job_id)
                manifests.append(self.ingestor.ingest(DAILY_BAR_SCHEMA, frame, provider, job_id))
                rows += len(frame)
            message = f"synced {len(open_dates)} trading days and {rows} bars"
            self.jobs.finish(job_id, "success", message, bars=rows, ingestion_batches=len(manifests))
            return {
                "trading_days": len(open_dates),
                "bars": rows,
                "securities": len(securities),
                "ingestion_batches": len(manifests),
            }
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class SyncCalendarUseCase:
    def __init__(
        self,
        repository: MarketRepository,
        market_provider: Callable[[], MarketDataProvider],
        ingestor: MarketBatchIngestor,
        jobs: JobController,
        clock: Clock,
    ):
        self.repository = repository
        self.market_provider = market_provider
        self.ingestor = ingestor
        self.jobs = jobs
        self.clock = clock

    def execute(self, at: datetime | None = None) -> dict[str, object]:
        at = at or self.clock.now()
        started = self.clock.now()
        job_id = self.jobs.start("sync-calendar", started)
        try:
            provider = self.market_provider()
            frame = validated_market_frame(
                TRADE_CALENDAR_SCHEMA,
                provider.fetch_trade_calendar(at.date() - timedelta(days=10), at.date() + timedelta(days=10)),
            )
            self.jobs.heartbeat(job_id)
            manifest = self.ingestor.ingest(TRADE_CALENDAR_SCHEMA, frame, provider, job_id)
            confirmed = self.repository.is_trading_day(at.date()) or bool(
                not frame.empty and (pd.to_datetime(frame["cal_date"]).dt.date == at.date()).any()
            )
            message = f"synced {len(frame)} calendar rows; today_confirmed={confirmed}"
            self.jobs.finish(job_id, "success", message, ingestion_batch_id=manifest.batch_id)
            return {"rows": len(frame), "today_confirmed": confirmed, "ingestion_batch_id": manifest.batch_id}
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class RunPrecloseUseCase:
    def __init__(
        self,
        settings: Settings,
        repository: ResearchRepository,
        live_provider: LiveQuoteProvider,
        news: NewsSynchronizer,
        ingestor: MarketBatchIngestor,
        point_in_time: PointInTimeDataGateway,
        strategy_registry: StrategyRegistry,
        strategy_runner: StrategyExecutionRunner,
        portfolio: PortfolioService,
        run_uow: ResearchRunUnitOfWork,
        manifests: RunManifestFactory,
        jobs: JobController,
        clock: Clock,
        observability: Observability | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.live_provider = live_provider
        self.news = news
        self.ingestor = ingestor
        self.point_in_time = point_in_time
        self.strategy_registry = strategy_registry
        self.strategy_runner = strategy_runner
        self.portfolio = portfolio
        self.run_uow = run_uow
        self.manifests = manifests
        self.jobs = jobs
        self.clock = clock
        self.observability = observability or get_observability()

    def execute(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        explicit_as_of = as_of is not None
        triggered_at = as_of or self.clock.now()
        if triggered_at.tzinfo is None:
            triggered_at = triggered_at.replace(tzinfo=SHANGHAI_TZ)
        strategy = self.strategy_registry.resolve(SignalKind.PRECLOSE_ENTRY)
        started = self.clock.now()
        scheduled_for = datetime.combine(
            triggered_at.date(),
            self.settings.schedule.preclose_run_at,
            tzinfo=SHANGHAI_TZ,
        )
        job_id = self.jobs.start("run-preclose", started, scheduled_for=scheduled_for)
        window_ok = self.settings.schedule.preclose_window.contains(triggered_at)
        if not self.repository.is_trading_day(triggered_at.date()):
            return self._missed(
                strategy,
                triggered_at,
                job_id,
                started,
                "交易日历未确认当日开市，禁止生成尾盘信号",
            )
        if not window_ok and not research_outside_window:
            return self._missed(
                strategy,
                triggered_at,
                job_id,
                started,
                f"不在{self.settings.schedule.preclose_window.describe()}窗口内，禁止补造尾盘信号",
            )
        try:
            signal_run_id = str(uuid4())
            quotes = validated_market_frame(LIVE_QUOTE_SCHEMA, self.live_provider.fetch_quotes())
            self.jobs.heartbeat(job_id)
            # Explicit as_of values define deterministic replay boundaries. A live
            # invocation freezes its boundary only after the quote response arrives.
            information_cutoff = triggered_at if explicit_as_of else self.clock.now()
            if (
                not explicit_as_of
                and not research_outside_window
                and not self.settings.schedule.preclose_window.contains(information_cutoff)
            ):
                return self._missed(
                    strategy,
                    information_cutoff,
                    job_id,
                    started,
                    f"实时行情采集完成时已超过{self.settings.schedule.preclose_window.describe()}窗口",
                )
            quote_times = (
                pd.to_datetime(quotes["captured_at"], utc=True)
                if not quotes.empty
                else pd.Series(dtype="datetime64[ns, UTC]")
            )
            cutoff_utc = pd.Timestamp(information_cutoff).tz_convert("UTC")
            quote_ages = (cutoff_utc - quote_times).dt.total_seconds()
            if not quote_times.empty:
                self.observability.metric(
                    MetricName.QUOTE_AGE_SECONDS,
                    float(quote_ages.min()),
                    unit="seconds",
                    signal_run_id=signal_run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=information_cutoff,
                    stage="preclose.quote-quality",
                )
            if not quote_ages.empty and bool((quote_ages < 0).any()):
                self.observability.alert(
                    AlertCode.QUOTE_DATA_FUTURE,
                    "pre-close live quote batch is after its frozen information boundary",
                    dedup_key=information_cutoff.date().isoformat(),
                    signal_run_id=signal_run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=information_cutoff,
                    stage="preclose.quote-quality",
                )
            # Fail before persistence, news synchronization, or LLM work when the
            # quote batch can never satisfy the point-in-time contract.
            self.point_in_time.validate_strategy_inputs(
                as_of=information_cutoff,
                information_cutoff=information_cutoff,
                securities=pd.DataFrame(),
                bars=pd.DataFrame(),
                quotes=quotes,
                risk_events=pd.DataFrame(),
            )
            quote_manifest = self.ingestor.ingest(LIVE_QUOTE_SCHEMA, quotes, self.live_provider, job_id)
            data_fresh = not quotes.empty and bool(
                ((quote_ages >= 0) & (quote_ages <= self.settings.quote_freshness_seconds)).all()
            )
            draft = SignalRun(
                **strategy.identity.attribution_fields(),
                run_id=signal_run_id,
                as_of=information_cutoff,
                signal_kind=SignalKind.PRECLOSE_ENTRY,
                information_cutoff=information_cutoff,
                status=RunStatus.RUNNING,
                lifecycle_state=ResearchRunState.CREATED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
            ).transition_to(ResearchRunState.COLLECTING_DATA)
            if not data_fresh:
                self.observability.alert(
                    AlertCode.QUOTE_DATA_STALE,
                    "pre-close live quote batch is missing or stale",
                    dedup_key=information_cutoff.date().isoformat(),
                    signal_run_id=draft.run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=information_cutoff,
                    stage="preclose.quote-quality",
                )
            news_fetch_start = datetime.combine(
                (information_cutoff - timedelta(days=10)).date(),
                time(0),
                tzinfo=SHANGHAI_TZ,
            )
            news = self.news.sync(
                news_fetch_start,
                information_cutoff,
                heartbeat=lambda: self.jobs.heartbeat(job_id),
                heartbeat_interval_seconds=_lease_heartbeat_interval_seconds(self.settings),
            )
            self.jobs.heartbeat(job_id)
            bars = self.repository.get_recent_daily_bars(
                150,
                as_of=information_cutoff.date() - timedelta(days=1),
            )
            expected_eod = self.repository.previous_trading_day(information_cutoff.date())
            latest_eod = pd.to_datetime(bars["trade_date"], errors="coerce").max() if not bars.empty else pd.NaT
            eod_fresh = bool(expected_eod and not pd.isna(latest_eod) and latest_eod.date() == expected_eod)
            if not eod_fresh:
                self.observability.alert(
                    AlertCode.EOD_DATA_STALE,
                    "official EOD data is not current for the pre-close Signal Run",
                    dedup_key=information_cutoff.date().isoformat(),
                    signal_run_id=draft.run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=information_cutoff,
                    stage="preclose.eod-quality",
                )
            if not pd.isna(latest_eod):
                self.observability.metric(
                    MetricName.LATEST_EOD_LAG_DAYS,
                    (information_cutoff.date() - latest_eod.date()).days,
                    unit="days",
                    signal_run_id=draft.run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=information_cutoff,
                    stage="preclose.eod-quality",
                )
            risk_start_date = self.repository.trading_day_lookback(
                information_cutoff.date(), self.settings.news_lookback_trading_days
            ) or (information_cutoff.date() - timedelta(days=10))
            risk_start = datetime.combine(risk_start_date, time(0), tzinfo=SHANGHAI_TZ)
            events = self.repository.get_risk_events(risk_start, information_cutoff)
            unprocessed = self.repository.unprocessed_official_codes(risk_start, information_cutoff)
            evaluation_at = information_cutoff if explicit_as_of else self.clock.now()
            if (
                not explicit_as_of
                and not research_outside_window
                and not self.settings.schedule.preclose_window.contains(evaluation_at)
            ):
                return self._missed(
                    strategy,
                    evaluation_at,
                    job_id,
                    started,
                    f"输入准备完成时已超过{self.settings.schedule.preclose_window.describe()}窗口",
                )
            provisional = draft.model_copy(
                update={
                    "as_of": evaluation_at,
                    "data_as_of": information_cutoff if data_fresh else None,
                    "data_fresh": data_fresh,
                    "official_news_healthy": news.official_healthy,
                    "mainstream_news_healthy": news.mainstream_healthy,
                }
            ).transition_to(ResearchRunState.EVALUATING)
            morning_run = self.repository.latest_signal_run(
                information_cutoff.date(),
                SignalKind.MORNING_WATCHLIST.value,
            )
            morning_codes: set[str] = set()
            morning_as_of: datetime | None = None
            if morning_run:
                morning_as_of = morning_run["as_of"]
                morning_frame = self.settings.selection.select_frame(
                    self.repository.get_candidates(str(morning_run["run_id"]), include_blocked=True)
                )
                morning_codes = set(morning_frame["ts_code"].astype(str)) if not morning_frame.empty else set()
            point_in_time = self.point_in_time.build_context(
                run_id=provisional.run_id,
                signal_kind=SignalKind.PRECLOSE_ENTRY,
                as_of=evaluation_at,
                information_cutoff=provisional.information_cutoff or information_cutoff,
                securities=self.repository.get_securities(),
                bars=bars,
                quotes=quotes,
                risk_events=events,
                unprocessed_official_codes=frozenset(unprocessed),
                previous_signal_codes=frozenset(morning_codes),
                previous_signal_as_of=morning_as_of,
                quote_ingestion_manifest=quote_manifest,
            )
            execution = self.strategy_runner.execute(point_in_time.context, expected_identity=strategy.identity)
            scored = execution.result
            self.jobs.heartbeat(job_id)
            publish_at = information_cutoff if explicit_as_of else self.clock.now()
            publish_window_ok = self.settings.schedule.preclose_window.contains(publish_at)
            if not publish_window_ok and not research_outside_window:
                return self._missed(
                    strategy,
                    publish_at,
                    job_id,
                    started,
                    f"策略计算完成时已超过{self.settings.schedule.preclose_window.describe()}窗口",
                )
            selected = self.settings.selection.select_candidates(scored.candidates)
            tradable = (
                window_ok
                and publish_window_ok
                and data_fresh
                and eod_fresh
                and news.official_healthy
                and bool(selected)
            )
            status = RunStatus.SUCCESS if tradable and news.mainstream_healthy else RunStatus.DEGRADED
            messages = list(news.messages)
            if not data_fresh:
                messages.append(f"实时行情缺失或超过{self.settings.quote_freshness_seconds}秒")
            if not eod_fresh:
                messages.append(f"正式日线未更新到前一交易日 {expected_eod or 'unknown'}")
            if research_outside_window and not window_ok:
                messages.append("窗口外研究运行，不生成模拟订单")
            run = provisional.model_copy(
                update={
                    "status": status,
                    "tradable": tradable,
                    "message": "; ".join(messages) or "ok",
                    "candidate_count": len([candidate for candidate in scored.candidates if not candidate.blocked]),
                    "metadata": {
                        "eligible_count": scored.eligible_count,
                        "exclusion_counts": scored.exclusion_counts,
                        "eod_fresh": eod_fresh,
                        "expected_eod": str(expected_eod) if expected_eod else None,
                        "news": news.__dict__,
                    },
                }
            ).transition_to(ResearchRunState.STAGED).transition_to(ResearchRunState.PUBLISHED)
            self.observability.metric(
                MetricName.CANDIDATE_COUNT,
                run.candidate_count,
                labels={"signal_kind": run.signal_kind.value},
                signal_run_id=run.run_id,
                strategy_id=run.strategy_id,
                strategy_version=run.strategy_version,
                information_cutoff=run.information_cutoff,
                stage="preclose.strategy-result",
            )
            orders = self.portfolio.plan_candidate_orders(run, scored.candidates)
            manifest = self.manifests.create(run, point_in_time.snapshots, execution.result_sha256)
            self.run_uow.commit(
                run,
                scored.candidates,
                orders,
                self.jobs.completion(
                    job_id,
                    "run-preclose",
                    started,
                    status.value,
                    run.message,
                    candidates=run.candidate_count,
                    orders=len(orders),
                    tradable=run.tradable,
                ),
                manifest,
            )
            return run
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise

    def _missed(self, strategy: Any, as_of: datetime, job_id: str, started: datetime, message: str) -> SignalRun:
        run = SignalRun(
            **strategy.identity.attribution_fields(),
            as_of=as_of,
            signal_kind=SignalKind.PRECLOSE_ENTRY,
            information_cutoff=as_of,
            status=RunStatus.MISSED,
            data_fresh=False,
            official_news_healthy=False,
            mainstream_news_healthy=False,
            tradable=False,
            message=message,
        )
        self.run_uow.commit(
            run,
            [],
            [],
            self.jobs.completion(job_id, "run-preclose", started, "missed", message),
        )
        return run


class RunMorningUseCase:
    def __init__(
        self,
        settings: Settings,
        repository: ResearchRepository,
        news: NewsSynchronizer,
        point_in_time: PointInTimeDataGateway,
        strategy_registry: StrategyRegistry,
        strategy_runner: StrategyExecutionRunner,
        run_uow: ResearchRunUnitOfWork,
        manifests: RunManifestFactory,
        jobs: JobController,
        clock: Clock,
        observability: Observability | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.news = news
        self.point_in_time = point_in_time
        self.strategy_registry = strategy_registry
        self.strategy_runner = strategy_runner
        self.run_uow = run_uow
        self.manifests = manifests
        self.jobs = jobs
        self.clock = clock
        self.observability = observability or get_observability()

    def execute(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        as_of = as_of or self.clock.now()
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=SHANGHAI_TZ)
        strategy = self.strategy_registry.resolve(SignalKind.MORNING_WATCHLIST)
        started = self.clock.now()
        job_id = self.jobs.start("run-morning", started)
        window_ok = self.settings.schedule.morning_window.contains(as_of)
        trading_day = self.repository.is_trading_day(as_of.date())
        if not trading_day or (not window_ok and not research_outside_window):
            message = (
                "交易日历未确认当日开市"
                if not trading_day
                else f"不在{self.settings.schedule.morning_window.describe()}窗口内"
            )
            run = SignalRun(
                **strategy.identity.attribution_fields(),
                as_of=as_of,
                signal_kind=SignalKind.MORNING_WATCHLIST,
                information_cutoff=as_of,
                status=RunStatus.MISSED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
                message=message,
            )
            self.run_uow.commit(
                run,
                [],
                [],
                self.jobs.completion(job_id, "run-morning", started, "missed", message),
            )
            return run
        try:
            draft = SignalRun(
                **strategy.identity.attribution_fields(),
                as_of=as_of,
                signal_kind=SignalKind.MORNING_WATCHLIST,
                information_cutoff=as_of,
                status=RunStatus.RUNNING,
                lifecycle_state=ResearchRunState.CREATED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
            ).transition_to(ResearchRunState.COLLECTING_DATA)
            news_start = datetime.combine((as_of - timedelta(days=10)).date(), time(0), tzinfo=SHANGHAI_TZ)
            news = self.news.sync(
                news_start,
                as_of,
                heartbeat=lambda: self.jobs.heartbeat(job_id),
                heartbeat_interval_seconds=_lease_heartbeat_interval_seconds(self.settings),
            )
            self.jobs.heartbeat(job_id)
            bars = self.repository.get_recent_daily_bars(150, as_of=as_of.date() - timedelta(days=1))
            expected_eod = self.repository.previous_trading_day(as_of.date())
            latest_eod = pd.to_datetime(bars["trade_date"], errors="coerce").max() if not bars.empty else pd.NaT
            eod_fresh = bool(expected_eod and not pd.isna(latest_eod) and latest_eod.date() == expected_eod)
            if not eod_fresh:
                self.observability.alert(
                    AlertCode.EOD_DATA_STALE,
                    "official EOD data is not current for the morning Signal Run",
                    dedup_key=as_of.date().isoformat(),
                    signal_run_id=draft.run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=as_of,
                    stage="morning.eod-quality",
                )
            if not pd.isna(latest_eod):
                self.observability.metric(
                    MetricName.LATEST_EOD_LAG_DAYS,
                    (as_of.date() - latest_eod.date()).days,
                    unit="days",
                    signal_run_id=draft.run_id,
                    strategy_id=strategy.identity.strategy_id,
                    strategy_version=strategy.identity.version,
                    information_cutoff=as_of,
                    stage="morning.eod-quality",
                )
            risk_start_date = self.repository.trading_day_lookback(
                as_of.date(), self.settings.news_lookback_trading_days
            )
            risk_start = datetime.combine(
                risk_start_date or (as_of.date() - timedelta(days=10)), time(0), tzinfo=SHANGHAI_TZ
            )
            events = self.repository.get_risk_events(risk_start, as_of)
            unprocessed = self.repository.unprocessed_official_codes(risk_start, as_of)
            provisional = draft.model_copy(
                update={
                    "data_as_of": datetime.combine(
                        expected_eod, self.settings.schedule.market_close, tzinfo=SHANGHAI_TZ
                    ) if expected_eod else None,
                    "data_fresh": eod_fresh,
                    "official_news_healthy": news.official_healthy,
                    "mainstream_news_healthy": news.mainstream_healthy,
                }
            ).transition_to(ResearchRunState.EVALUATING)
            point_in_time = self.point_in_time.build_context(
                run_id=provisional.run_id,
                signal_kind=SignalKind.MORNING_WATCHLIST,
                as_of=as_of,
                information_cutoff=provisional.information_cutoff or as_of,
                securities=self.repository.get_securities(),
                bars=bars,
                risk_events=events,
                unprocessed_official_codes=frozenset(unprocessed),
            )
            execution = self.strategy_runner.execute(point_in_time.context, expected_identity=strategy.identity)
            scored = execution.result
            self.jobs.heartbeat(job_id)
            status = RunStatus.SUCCESS if eod_fresh and news.official_healthy else RunStatus.DEGRADED
            messages = list(news.messages)
            if not eod_fresh:
                messages.append(f"正式日线未更新到前一交易日 {expected_eod or 'unknown'}")
            if research_outside_window and not window_ok:
                messages.append("窗口外研究运行")
            run = provisional.model_copy(
                update={
                    "status": status,
                    "message": "; ".join(messages) or "ok",
                    "candidate_count": len([item for item in scored.candidates if not item.blocked]),
                    "metadata": {
                        "eligible_count": scored.eligible_count,
                        "news": news.__dict__,
                        "eod_fresh": eod_fresh,
                    },
                }
            ).transition_to(ResearchRunState.STAGED).transition_to(ResearchRunState.PUBLISHED)
            self.observability.metric(
                MetricName.CANDIDATE_COUNT,
                run.candidate_count,
                labels={"signal_kind": run.signal_kind.value},
                signal_run_id=run.run_id,
                strategy_id=run.strategy_id,
                strategy_version=run.strategy_version,
                information_cutoff=run.information_cutoff,
                stage="morning.strategy-result",
            )
            self.run_uow.commit(
                run,
                scored.candidates,
                [],
                self.jobs.completion(
                    job_id,
                    "run-morning",
                    started,
                    status.value,
                    run.message,
                    candidates=run.candidate_count,
                ),
                self.manifests.create(run, point_in_time.snapshots, execution.result_sha256),
            )
            return run
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class EvaluateMorningUseCase:
    def __init__(
        self,
        repository: ResearchRepository,
        evaluation: CandidateEvaluationService,
        jobs: JobController,
        clock: Clock,
    ):
        self.repository = repository
        self.evaluation = evaluation
        self.jobs = jobs
        self.clock = clock

    def execute(self, at: datetime | None = None) -> list[object]:
        at = at or self.clock.now()
        started = self.clock.now()
        job_id = self.jobs.start("evaluate-morning", started)
        try:
            run = self.repository.latest_signal_run(at.date(), SignalKind.MORNING_WATCHLIST.value)
            outcomes = self.evaluation.evaluate(run, at) if run and run["status"] in {"success", "degraded"} else []
            self.jobs.finish(job_id, "success", f"evaluated {len(outcomes)} candidates")
            return outcomes
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class EvaluatePreviousPrecloseUseCase:
    def __init__(self, repository: ResearchRepository, evaluation: CandidateEvaluationService, clock: Clock):
        self.repository = repository
        self.evaluation = evaluation
        self.clock = clock

    def execute(self, at: datetime | None = None) -> list[object]:
        at = at or self.clock.now()
        previous = self.repository.previous_trading_day(at.date())
        run = self.repository.latest_signal_run(previous, SignalKind.PRECLOSE_ENTRY.value) if previous else None
        return self.evaluation.evaluate(run, at) if run and run["status"] in {"success", "degraded"} else []


class CaptureFillUseCase:
    def __init__(
        self,
        settings: Settings,
        repository: ResearchRepository,
        live_provider: LiveQuoteProvider,
        ingestor: MarketBatchIngestor,
        portfolio: PortfolioService,
        jobs: JobController,
        clock: Clock,
    ):
        self.settings = settings
        self.repository = repository
        self.live_provider = live_provider
        self.ingestor = ingestor
        self.portfolio = portfolio
        self.jobs = jobs
        self.clock = clock

    def execute(self, at: datetime | None = None) -> list[object]:
        at = at or self.clock.now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = self.clock.now()
        job_id = self.jobs.start("capture-fill", started)
        if not self.settings.schedule.fill_window.contains(at):
            self.jobs.finish(job_id, "missed", f"不在{self.settings.schedule.fill_window.describe()}成交窗口")
            return []
        try:
            run = self.repository.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
            if not run or not bool(run["tradable"]):
                self.jobs.finish(job_id, "degraded", "没有可交易的当日信号")
                return []
            quotes = validated_market_frame(LIVE_QUOTE_SCHEMA, self.live_provider.fetch_quotes())
            self.jobs.heartbeat(job_id)
            self.ingestor.ingest(LIVE_QUOTE_SCHEMA, quotes, self.live_provider, job_id)
            fills = self.portfolio.capture_buy_fills(str(run["run_id"]), at, quotes)
            self.jobs.finish(job_id, "success", f"filled {len(fills)} orders")
            return fills
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class MonitorPaperUseCase:
    def __init__(
        self,
        settings: Settings,
        repository: ResearchRepository,
        portfolio: PortfolioService,
        jobs: JobController,
        clock: Clock,
    ):
        self.settings = settings
        self.repository = repository
        self.portfolio = portfolio
        self.jobs = jobs
        self.clock = clock

    def execute(self, at: datetime | None = None) -> list[object]:
        at = at or self.clock.now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = self.clock.now()
        job_id = self.jobs.start("monitor-paper", started)
        if not self.repository.is_trading_day(at.date()) or not self.settings.schedule.is_trading_session(at):
            self.jobs.finish(job_id, "success", "outside trading session; no-op")
            return []
        try:
            self.jobs.heartbeat(job_id)
            fills = self.portfolio.monitor_positions(at)
            self.jobs.finish(job_id, "success", f"closed {len(fills)} positions")
            return fills
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class RunPostcloseUseCase:
    def __init__(
        self,
        settings: Settings,
        repository: ResearchRepository,
        news: NewsSynchronizer,
        evaluate_morning: EvaluateMorningUseCase,
        evaluate_previous_preclose: EvaluatePreviousPrecloseUseCase,
        report_service: Callable[[], ReportService],
        jobs: JobController,
        clock: Clock,
    ):
        self.settings = settings
        self.repository = repository
        self.news = news
        self.evaluate_morning = evaluate_morning
        self.evaluate_previous_preclose = evaluate_previous_preclose
        self.report_service = report_service
        self.jobs = jobs
        self.clock = clock

    def execute(self, at: datetime | None = None) -> str:
        at = at or self.clock.now()
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = self.clock.now()
        job_id = self.jobs.start("run-postclose", started)
        try:
            run = self.repository.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
            run_time = run["as_of"] if run else datetime.combine(
                at.date(), self.settings.schedule.preclose_run_at, tzinfo=SHANGHAI_TZ
            )
            news_start = datetime.combine((at - timedelta(days=10)).date(), time(0), tzinfo=SHANGHAI_TZ)
            news = self.news.sync(
                news_start,
                at,
                heartbeat=lambda: self.jobs.heartbeat(job_id),
                heartbeat_interval_seconds=_lease_heartbeat_interval_seconds(self.settings),
            )
            self.jobs.heartbeat(job_id)
            self.evaluate_morning.execute(at)
            self.evaluate_previous_preclose.execute(at)
            self.jobs.heartbeat(job_id)
            candidates = self.repository.get_candidates(str(run["run_id"])) if run else pd.DataFrame()
            events = self.repository.get_risk_events(run_time, at)
            context = {
                "report_date": at.date().isoformat(),
                "signal_as_of": str(run_time),
                "preclose_label": self.settings.schedule.preclose_run_at.strftime("%H:%M"),
                "candidates": self.settings.selection.select_frame(candidates).to_dict("records"),
                "positions": self.repository.get_open_positions().to_dict("records"),
                "cash": self.repository.get_cash(),
                "after_entry_events": events.to_dict("records"),
                "news_health": news.__dict__,
                "required_disclaimer": (
                    "收盘后未知公告无法由"
                    f"{self.settings.schedule.preclose_run_at:%H:%M}系统提前预测，属于不可消除的隔夜风险。"
                ),
            }
            report = self.report_service().generate(at.date(), at, context)
            self.jobs.finish(job_id, "success", "report generated")
            return report
        except Exception as exc:
            self.jobs.finish(job_id, "failed", str(exc))
            raise


class RecoverExpiredJobsUseCase:
    def __init__(self, repository: JobRepository, clock: Clock):
        self.repository = repository
        self.clock = clock

    def execute(self, at: datetime | None = None) -> list[str]:
        return self.repository.recover_expired_jobs(at or self.clock.now())


@dataclass(frozen=True)
class WorkflowUseCases:
    doctor: DoctorUseCase
    sync_eod: SyncEodUseCase
    sync_calendar: SyncCalendarUseCase
    run_preclose: RunPrecloseUseCase
    run_morning: RunMorningUseCase
    evaluate_morning: EvaluateMorningUseCase
    evaluate_previous_preclose: EvaluatePreviousPrecloseUseCase
    capture_fill: CaptureFillUseCase
    monitor_paper: MonitorPaperUseCase
    run_postclose: RunPostcloseUseCase
    recover_expired_jobs: RecoverExpiredJobsUseCase
