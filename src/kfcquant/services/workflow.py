from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from sys import version as PYTHON_VERSION
from sys import version_info as PYTHON_VERSION_INFO
from typing import Any
from uuid import uuid4

import pandas as pd

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.db import Database
from kfcquant.interfaces import LiveQuoteProvider, LLMProvider, MarketDataProvider, NewsProvider
from kfcquant.models import ResearchRunState, RunStatus, SignalKind, SignalRun
from kfcquant.providers.document_loader import DocumentLoader
from kfcquant.providers.factory import (
    build_live_provider,
    build_llm_provider,
    build_market_provider,
    build_news_provider,
)
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.news import NewsService, NewsSyncResult
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.reports import ReportService
from kfcquant.services.scoring import ScoringService
from kfcquant.unit_of_work import (
    DuckDBResearchRunUnitOfWork,
    JobCompletion,
    ResearchRunUnitOfWork,
)

MINIMUM_PYTHON_VERSION = (3, 12)


class Workflow:
    def __init__(
        self,
        settings: Settings,
        database: Database | None = None,
        market_provider: MarketDataProvider | None = None,
        live_provider: LiveQuoteProvider | None = None,
        news_provider: NewsProvider | None = None,
        llm_provider: LLMProvider | None = None,
        run_uow: ResearchRunUnitOfWork | None = None,
    ):
        self.settings = settings
        self.database = database or Database(
            settings.database_path,
            settings.initial_cash,
            settings.database_lock_timeout_seconds,
            settings.runtime_dir / "database.lock",
        )
        self.database.initialize()
        self.live_provider = live_provider or build_live_provider(settings)
        self._market_provider = market_provider
        self._news_provider = news_provider
        if (
            news_provider is None
            and market_provider is not None
            and hasattr(market_provider, "fetch_official_documents")
        ):
            self._news_provider = market_provider  # Backward-compatible composite test/provider.
        self._llm_provider = llm_provider
        self.scoring = ScoringService(settings)
        self.portfolio = PortfolioService(self.database, settings, self.live_provider)
        self.evaluation = CandidateEvaluationService(self.database, settings, self.live_provider)
        self.run_uow = run_uow or DuckDBResearchRunUnitOfWork(self.database)

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

    def _raw_snapshot(self, provider: str, kind: str, at: datetime | date, frame: pd.DataFrame) -> Path | None:
        if frame.empty:
            return None
        stamp = at if isinstance(at, datetime) else datetime.combine(at, time(0, 0), tzinfo=SHANGHAI_TZ)
        directory = self.settings.raw_data_dir / provider / kind / stamp.strftime("%Y%m%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stamp.strftime('%H%M%S')}.parquet"
        frame.to_parquet(path, index=False)
        return path

    def _raw_range_snapshot(
        self,
        provider: str,
        kind: str,
        start: date,
        end: date,
        frame: pd.DataFrame,
    ) -> Path | None:
        if frame.empty:
            return None
        first_code = str(frame.iloc[0].get("ts_code", "market")).replace(".", "-")
        last_code = str(frame.iloc[-1].get("ts_code", first_code)).replace(".", "-")
        code = first_code if first_code == last_code else f"{first_code}_{last_code}"
        directory = self.settings.raw_data_dir / provider / kind / f"{start:%Y%m%d}-{end:%Y%m%d}"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d%H%M%S%f")
        path = directory / f"{code}-{stamp}.parquet"
        frame.to_parquet(path, index=False)
        return path

    def _job_start(self, name: str, at: datetime) -> str:
        job_id = str(uuid4())
        self.database.record_job(job_id, name, at, "running", "started")
        return job_id

    def _job_finish(
        self, job_id: str, name: str, started: datetime, status: str, message: str, **metadata: Any
    ) -> None:
        self.database.record_job(
            job_id, name, started, status, message, finished_at=datetime.now(SHANGHAI_TZ), metadata=metadata
        )

    @staticmethod
    def _job_completion(
        job_id: str,
        name: str,
        started: datetime,
        status: str,
        message: str,
        **metadata: Any,
    ) -> JobCompletion:
        return JobCompletion(
            job_run_id=job_id,
            job_name=name,
            started_at=started,
            finished_at=datetime.now(SHANGHAI_TZ),
            status=status,
            message=message,
            metadata=metadata,
        )

    def doctor(self, online: bool = False) -> list[dict[str, object]]:
        checks: list[dict[str, object]] = []
        checks.append(
            {
                "check": "python",
                "ok": PYTHON_VERSION_INFO >= MINIMUM_PYTHON_VERSION,
                "detail": PYTHON_VERSION.split()[0],
            }
        )
        checks.append(
            {
                "check": "database",
                "ok": self.settings.database_path.parent.exists(),
                "detail": str(self.settings.database_path),
            }
        )
        checks.append({"check": "data-profile", "ok": True, "detail": self.settings.data_profile})
        checks.append(
            {
                "check": "market-provider",
                "ok": self.settings.market_provider.lower() in {"baostock", "tushare"},
                "detail": self.settings.market_provider,
            }
        )
        checks.append(
            {
                "check": "news-provider",
                "ok": self.settings.news_provider.lower() in {"akshare", "tushare"},
                "detail": self.settings.news_provider,
            }
        )
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
        if online:
            try:
                today = datetime.now(SHANGHAI_TZ).date()
                frame = self.market_provider.fetch_trade_calendar(today - timedelta(days=2), today)
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
                frame = self.live_provider.fetch_quotes()
                checks.append({"check": "akshare-online", "ok": not frame.empty, "detail": f"{len(frame)} quotes"})
            except Exception as exc:
                checks.append({"check": "akshare-online", "ok": False, "detail": str(exc)})
            try:
                now = datetime.now(SHANGHAI_TZ)
                documents = self.news_provider.fetch_official_documents(now - timedelta(days=1), now)
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
                    provider = self.llm_provider
                    healthcheck = getattr(provider, "healthcheck", None)
                    detail = healthcheck() if healthcheck else "provider created"
                    checks.append({"check": f"{self.settings.llm_provider}-online", "ok": True, "detail": detail})
                except Exception as exc:
                    checks.append({"check": f"{self.settings.llm_provider}-online", "ok": False, "detail": str(exc)})
        return checks

    def sync_eod(self, start: date, end: date) -> dict[str, object]:
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("sync-eod", started)
        try:
            securities = self.market_provider.fetch_securities()
            self.database.upsert_securities(securities)
            calendar = self.market_provider.fetch_trade_calendar(start, end)
            self.database.upsert_trade_calendar(calendar)
            open_dates = [row["cal_date"] for row in calendar.to_dict("records") if bool(row["is_open"])]
            rows = 0
            range_loader = getattr(self.market_provider, "iter_daily_range", None)
            provider_name = str(getattr(self.market_provider, "source_name", self.settings.market_provider))
            if range_loader:
                list_dates = pd.to_datetime(securities["list_date"], errors="coerce")
                delist_dates = pd.to_datetime(securities["delist_date"], errors="coerce")
                eligible = securities[
                    (list_dates <= pd.Timestamp(end)) & (delist_dates.isna() | (delist_dates >= pd.Timestamp(start)))
                ]
                for frame in range_loader(start, end, eligible["ts_code"].astype(str).tolist()):
                    self.database.upsert_daily_bars(frame)
                    self._raw_range_snapshot(provider_name, "daily", start, end, frame)
                    rows += len(frame)
            else:
                for trade_date in open_dates:
                    frame = self.market_provider.fetch_daily(trade_date)
                    self.database.upsert_daily_bars(frame)
                    self._raw_snapshot(provider_name, "daily", trade_date, frame)
                    rows += len(frame)
            message = f"synced {len(open_dates)} trading days and {rows} bars"
            self._job_finish(job_id, "sync-eod", started, "success", message, bars=rows)
            return {"trading_days": len(open_dates), "bars": rows, "securities": len(securities)}
        except Exception as exc:
            self._job_finish(job_id, "sync-eod", started, "failed", str(exc))
            raise

    def sync_calendar(self, at: datetime | None = None) -> dict[str, object]:
        at = at or datetime.now(SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("sync-calendar", started)
        try:
            frame = self.market_provider.fetch_trade_calendar(
                at.date() - timedelta(days=10), at.date() + timedelta(days=10)
            )
            self.database.upsert_trade_calendar(frame)
            confirmed = self.database.is_trading_day(at.date()) or bool(
                not frame.empty and (pd.to_datetime(frame["cal_date"]).dt.date == at.date()).any()
            )
            message = f"synced {len(frame)} calendar rows; today_confirmed={confirmed}"
            self._job_finish(job_id, "sync-calendar", started, "success", message)
            return {"rows": len(frame), "today_confirmed": confirmed}
        except Exception as exc:
            self._job_finish(job_id, "sync-calendar", started, "failed", str(exc))
            raise

    def _sync_news(self, start: datetime, end: datetime) -> NewsSyncResult:
        try:
            provider = self.news_provider
        except Exception as exc:
            return NewsSyncResult(False, False, 0, 0, 0, 0, [str(exc)])
        service = NewsService(
            self.database,
            provider,
            self.optional_llm(),
            DocumentLoader(self.settings.max_document_bytes),
        )
        return service.sync(start, end)

    def run_preclose(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        as_of = as_of or datetime.now(SHANGHAI_TZ)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("run-preclose", started)
        window_ok = self.settings.schedule.preclose_window.contains(as_of)
        trading_day = self.database.is_trading_day(as_of.date())
        if not trading_day:
            run = SignalRun(
                as_of=as_of,
                signal_kind=SignalKind.PRECLOSE_ENTRY,
                strategy_version=self.settings.strategy_version_preclose,
                information_cutoff=as_of,
                status=RunStatus.MISSED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
                message="交易日历未确认当日开市，禁止生成尾盘信号",
            )
            self.run_uow.commit(
                run,
                [],
                [],
                self._job_completion(job_id, "run-preclose", started, "missed", run.message),
            )
            return run
        if not window_ok and not research_outside_window:
            run = SignalRun(
                as_of=as_of,
                signal_kind=SignalKind.PRECLOSE_ENTRY,
                strategy_version=self.settings.strategy_version_preclose,
                information_cutoff=as_of,
                status=RunStatus.MISSED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
                message=f"不在{self.settings.schedule.preclose_window.describe()}窗口内，禁止补造尾盘信号",
            )
            self.run_uow.commit(
                run,
                [],
                [],
                self._job_completion(job_id, "run-preclose", started, "missed", run.message),
            )
            return run
        try:
            draft = SignalRun(
                as_of=as_of,
                signal_kind=SignalKind.PRECLOSE_ENTRY,
                strategy_version=self.settings.strategy_version_preclose,
                information_cutoff=as_of,
                status=RunStatus.RUNNING,
                lifecycle_state=ResearchRunState.CREATED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
            ).transition_to(ResearchRunState.COLLECTING_DATA)
            quotes = self.live_provider.fetch_quotes()
            self.database.insert_live_quotes(quotes)
            self._raw_snapshot("akshare", "quotes", as_of, quotes)
            quote_times = (
                pd.to_datetime(quotes["captured_at"], utc=True)
                if not quotes.empty
                else pd.Series(dtype="datetime64[ns, UTC]")
            )
            as_of_utc = pd.Timestamp(as_of).tz_convert("UTC")
            data_fresh = not quotes.empty and bool(
                ((as_of_utc - quote_times).abs().dt.total_seconds() <= self.settings.quote_freshness_seconds).all()
            )

            news_fetch_start = datetime.combine((as_of - timedelta(days=10)).date(), time(0, 0), tzinfo=SHANGHAI_TZ)
            news = self._sync_news(news_fetch_start, as_of)
            bars = self.database.get_recent_daily_bars(150, as_of=as_of.date() - timedelta(days=1))
            expected_eod = self.database.previous_trading_day(as_of.date())
            latest_eod = pd.to_datetime(bars["trade_date"], errors="coerce").max() if not bars.empty else pd.NaT
            eod_fresh = bool(expected_eod and not pd.isna(latest_eod) and latest_eod.date() == expected_eod)
            securities = self.database.get_securities()
            risk_start_date = self.database.trading_day_lookback(
                as_of.date(), self.settings.news_lookback_trading_days
            ) or (as_of.date() - timedelta(days=10))
            risk_start = datetime.combine(risk_start_date, time(0, 0), tzinfo=SHANGHAI_TZ)
            events = self.database.get_risk_events(risk_start, as_of)
            unprocessed = self.database.unprocessed_official_codes(risk_start, as_of)

            provisional = draft.model_copy(
                update={
                    "data_as_of": as_of if data_fresh else None,
                    "data_fresh": data_fresh,
                    "official_news_healthy": news.official_healthy,
                    "mainstream_news_healthy": news.mainstream_healthy,
                }
            ).transition_to(ResearchRunState.EVALUATING)
            morning_run = self.database.latest_signal_run(as_of.date(), SignalKind.MORNING_WATCHLIST.value)
            morning_codes: set[str] = set()
            if morning_run:
                morning_frame = self.database.get_candidates(
                    str(morning_run["run_id"]), include_blocked=False
                ).head(self.settings.selection.top_n)
                morning_codes = set(morning_frame["ts_code"].astype(str)) if not morning_frame.empty else set()
            scored = self.scoring.score(
                provisional.run_id,
                securities,
                bars,
                quotes,
                as_of,
                events,
                unprocessed,
                morning_codes,
            )
            tradable = window_ok and data_fresh and eod_fresh and news.official_healthy and bool(scored.candidates)
            status = RunStatus.SUCCESS if tradable and news.mainstream_healthy else RunStatus.DEGRADED
            message_parts = news.messages.copy()
            if not data_fresh:
                message_parts.append(f"实时行情缺失或超过{self.settings.quote_freshness_seconds}秒")
            if not eod_fresh:
                message_parts.append(f"正式日线未更新到前一交易日 {expected_eod or 'unknown'}")
            if research_outside_window and not window_ok:
                message_parts.append("窗口外研究运行，不生成模拟订单")
            staged = provisional.model_copy(
                update={
                    "status": status,
                    "tradable": tradable,
                    "message": "; ".join(message_parts) or "ok",
                    "candidate_count": len([candidate for candidate in scored.candidates if not candidate.blocked]),
                    "metadata": {
                        "eligible_count": scored.eligible_count,
                        "exclusion_counts": scored.exclusion_counts,
                        "eod_fresh": eod_fresh,
                        "expected_eod": str(expected_eod) if expected_eod else None,
                        "news": news.__dict__,
                    },
                }
            ).transition_to(ResearchRunState.STAGED)
            run = staged.transition_to(ResearchRunState.PUBLISHED)
            orders = self.portfolio.plan_candidate_orders(run, scored.candidates)
            self.run_uow.commit(
                run,
                scored.candidates,
                orders,
                self._job_completion(
                    job_id,
                    "run-preclose",
                    started,
                    status.value,
                    run.message,
                    candidates=run.candidate_count,
                    orders=len(orders),
                    tradable=run.tradable,
                ),
            )
            return run
        except Exception as exc:
            self._job_finish(job_id, "run-preclose", started, "failed", str(exc))
            raise

    def run_morning(self, as_of: datetime | None = None, research_outside_window: bool = False) -> SignalRun:
        as_of = as_of or datetime.now(SHANGHAI_TZ)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("run-morning", started)
        window_ok = self.settings.schedule.morning_window.contains(as_of)
        if not self.database.is_trading_day(as_of.date()) or (not window_ok and not research_outside_window):
            message = (
                "交易日历未确认当日开市"
                if not self.database.is_trading_day(as_of.date())
                else f"不在{self.settings.schedule.morning_window.describe()}窗口内"
            )
            run = SignalRun(
                as_of=as_of,
                signal_kind=SignalKind.MORNING_WATCHLIST,
                strategy_version=self.settings.strategy_version_morning,
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
                self._job_completion(job_id, "run-morning", started, "missed", message),
            )
            return run
        try:
            draft = SignalRun(
                as_of=as_of,
                signal_kind=SignalKind.MORNING_WATCHLIST,
                strategy_version=self.settings.strategy_version_morning,
                information_cutoff=as_of,
                status=RunStatus.RUNNING,
                lifecycle_state=ResearchRunState.CREATED,
                data_fresh=False,
                official_news_healthy=False,
                mainstream_news_healthy=False,
                tradable=False,
            ).transition_to(ResearchRunState.COLLECTING_DATA)
            news_start = datetime.combine((as_of - timedelta(days=10)).date(), time(0), tzinfo=SHANGHAI_TZ)
            news = self._sync_news(news_start, as_of)
            bars = self.database.get_recent_daily_bars(150, as_of=as_of.date() - timedelta(days=1))
            expected_eod = self.database.previous_trading_day(as_of.date())
            latest_eod = pd.to_datetime(bars["trade_date"], errors="coerce").max() if not bars.empty else pd.NaT
            eod_fresh = bool(expected_eod and not pd.isna(latest_eod) and latest_eod.date() == expected_eod)
            risk_start_date = self.database.trading_day_lookback(as_of.date(), self.settings.news_lookback_trading_days)
            risk_start = datetime.combine(
                risk_start_date or (as_of.date() - timedelta(days=10)), time(0), tzinfo=SHANGHAI_TZ
            )
            events = self.database.get_risk_events(risk_start, as_of)
            unprocessed = self.database.unprocessed_official_codes(risk_start, as_of)
            provisional = draft.model_copy(
                update={
                    "data_as_of": datetime.combine(
                        expected_eod, self.settings.schedule.market_close, tzinfo=SHANGHAI_TZ
                    )
                    if expected_eod
                    else None,
                    "data_fresh": eod_fresh,
                    "official_news_healthy": news.official_healthy,
                    "mainstream_news_healthy": news.mainstream_healthy,
                }
            ).transition_to(ResearchRunState.EVALUATING)
            scored = self.scoring.score_morning(
                provisional.run_id,
                self.database.get_securities(),
                bars,
                as_of,
                events,
                unprocessed,
            )
            status = RunStatus.SUCCESS if eod_fresh and news.official_healthy else RunStatus.DEGRADED
            messages = list(news.messages)
            if not eod_fresh:
                messages.append(f"正式日线未更新到前一交易日 {expected_eod or 'unknown'}")
            if research_outside_window and not window_ok:
                messages.append("窗口外研究运行")
            staged = provisional.model_copy(
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
            ).transition_to(ResearchRunState.STAGED)
            run = staged.transition_to(ResearchRunState.PUBLISHED)
            self.run_uow.commit(
                run,
                scored.candidates,
                [],
                self._job_completion(
                    job_id,
                    "run-morning",
                    started,
                    status.value,
                    run.message,
                    candidates=run.candidate_count,
                ),
            )
            return run
        except Exception as exc:
            self._job_finish(job_id, "run-morning", started, "failed", str(exc))
            raise

    def evaluate_morning(self, at: datetime | None = None) -> list[object]:
        at = at or datetime.now(SHANGHAI_TZ)
        run = self.database.latest_signal_run(at.date(), SignalKind.MORNING_WATCHLIST.value)
        return self.evaluation.evaluate(run, at) if run and run["status"] in {"success", "degraded"} else []

    def evaluate_previous_preclose(self, at: datetime | None = None) -> list[object]:
        at = at or datetime.now(SHANGHAI_TZ)
        previous = self.database.previous_trading_day(at.date())
        run = self.database.latest_signal_run(previous, SignalKind.PRECLOSE_ENTRY.value) if previous else None
        return self.evaluation.evaluate(run, at) if run and run["status"] in {"success", "degraded"} else []

    def capture_fill(self, at: datetime | None = None) -> list[object]:
        at = at or datetime.now(SHANGHAI_TZ)
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("capture-fill", started)
        if not self.settings.schedule.fill_window.contains(at):
            self._job_finish(
                job_id,
                "capture-fill",
                started,
                "missed",
                f"不在{self.settings.schedule.fill_window.describe()}成交窗口",
            )
            return []
        try:
            run = self.database.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
            if not run or not bool(run["tradable"]):
                self._job_finish(job_id, "capture-fill", started, "degraded", "没有可交易的当日信号")
                return []
            quotes = self.live_provider.fetch_quotes()
            self.database.insert_live_quotes(quotes)
            self._raw_snapshot("akshare", "quotes", at, quotes)
            fills = self.portfolio.capture_buy_fills(str(run["run_id"]), at, quotes)
            self._job_finish(job_id, "capture-fill", started, "success", f"filled {len(fills)} orders")
            return fills
        except Exception as exc:
            self._job_finish(job_id, "capture-fill", started, "failed", str(exc))
            raise

    def _in_trading_session(self, at: datetime) -> bool:
        return self.settings.schedule.is_trading_session(at)

    def monitor_paper(self, at: datetime | None = None) -> list[object]:
        at = at or datetime.now(SHANGHAI_TZ)
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("monitor-paper", started)
        if not self.database.is_trading_day(at.date()) or not self._in_trading_session(at):
            self._job_finish(job_id, "monitor-paper", started, "success", "outside trading session; no-op")
            return []
        try:
            fills = self.portfolio.monitor_positions(at)
            self._job_finish(job_id, "monitor-paper", started, "success", f"closed {len(fills)} positions")
            return fills
        except Exception as exc:
            self._job_finish(job_id, "monitor-paper", started, "failed", str(exc))
            raise

    def run_postclose(self, at: datetime | None = None) -> str:
        at = at or datetime.now(SHANGHAI_TZ)
        if at.tzinfo is None:
            at = at.replace(tzinfo=SHANGHAI_TZ)
        started = datetime.now(SHANGHAI_TZ)
        job_id = self._job_start("run-postclose", started)
        try:
            run = self.database.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
            run_time = (
                run["as_of"]
                if run
                else datetime.combine(at.date(), self.settings.schedule.preclose_run_at, tzinfo=SHANGHAI_TZ)
            )
            news_start = datetime.combine((at - timedelta(days=10)).date(), time(0, 0), tzinfo=SHANGHAI_TZ)
            news = self._sync_news(news_start, at)
            self.evaluate_morning(at)
            self.evaluate_previous_preclose(at)
            candidates = self.database.get_candidates(str(run["run_id"])) if run else pd.DataFrame()
            events = self.database.get_risk_events(run_time, at)
            context = {
                "report_date": at.date().isoformat(),
                "signal_as_of": str(run_time),
                "preclose_label": self.settings.schedule.preclose_run_at.strftime("%H:%M"),
                "candidates": candidates.head(self.settings.selection.top_n).to_dict("records"),
                "positions": self.database.get_open_positions().to_dict("records"),
                "cash": self.database.get_cash(),
                "after_entry_events": events.to_dict("records"),
                "news_health": news.__dict__,
                "required_disclaimer": (
                    "收盘后未知公告无法由"
                    f"{self.settings.schedule.preclose_run_at:%H:%M}系统提前预测，属于不可消除的隔夜风险。"
                ),
            }
            report = ReportService(
                self.database, self.optional_llm(), self.settings.report_dir, self.settings.llm_report_model
            ).generate(at.date(), at, context)
            self._job_finish(job_id, "run-postclose", started, "success", "report generated")
            return report
        except Exception as exc:
            self._job_finish(job_id, "run-postclose", started, "failed", str(exc))
            raise
