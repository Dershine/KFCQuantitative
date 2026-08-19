from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from kfcquant.application.errors import JobLeaseLostError
from kfcquant.application.use_cases import (
    CaptureFillUseCase,
    EvaluateMorningUseCase,
    EvaluatePreviousPrecloseUseCase,
    JobController,
    MonitorPaperUseCase,
    NewsSynchronizer,
    RecoverExpiredJobsUseCase,
    RunPostcloseUseCase,
    SyncCalendarUseCase,
)
from kfcquant.clock import ReplayClock
from kfcquant.config import SHANGHAI_TZ
from kfcquant.services.news import NewsSyncResult

AT = datetime(2026, 8, 10, 14, 45, tzinfo=SHANGHAI_TZ)


class FakeJobs:
    def __init__(self):
        self.finishes: list[tuple[str, str, str]] = []
        self.heartbeats: list[str] = []

    def start(self, name, at):
        return f"job-{name}"

    def heartbeat(self, job_id):
        self.heartbeats.append(job_id)
        return AT

    def finish(self, job_id, status, message, **metadata):
        self.finishes.append((job_id, status, message))


def test_job_controller_fails_closed_when_lease_cannot_be_renewed(settings):
    repository = Mock()
    repository.heartbeat_job.return_value = False
    jobs = JobController(repository, settings, ReplayClock(AT))

    with pytest.raises(JobLeaseLostError, match="job-id"):
        jobs.heartbeat("job-id")


def test_news_synchronizer_converts_construction_failure_to_unhealthy_result():
    def fail():
        raise RuntimeError("provider unavailable")

    result = NewsSynchronizer(fail).sync(AT, AT)

    assert not result.official_healthy
    assert not result.mainstream_healthy
    assert result.messages == ["provider unavailable"]


def test_calendar_use_case_has_an_independent_repository_boundary():
    provider = Mock()
    provider.fetch_trade_calendar.return_value = pd.DataFrame(
        [{"cal_date": AT.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}]
    )
    repository = Mock()
    repository.is_trading_day.return_value = False
    ingestor = Mock()
    ingestor.ingest.return_value = SimpleNamespace(batch_id="calendar-batch")
    jobs = FakeJobs()

    result = SyncCalendarUseCase(
        repository,
        lambda: provider,
        ingestor,
        jobs,
        ReplayClock(AT),
    ).execute(AT)

    assert result == {"rows": 1, "today_confirmed": True, "ingestion_batch_id": "calendar-batch"}
    assert jobs.finishes[-1][1] == "success"


def test_fill_and_monitor_use_cases_keep_runtime_gates_outside_portfolio(settings):
    jobs = FakeJobs()
    repository = Mock()
    portfolio = Mock()
    live = Mock()
    ingestor = Mock()
    outside = AT.replace(hour=16)

    fills = CaptureFillUseCase(
        settings,
        repository,
        live,
        ingestor,
        portfolio,
        jobs,
        ReplayClock(outside),
    ).execute(outside)
    repository.is_trading_day.return_value = False
    monitored = MonitorPaperUseCase(
        settings,
        repository,
        portfolio,
        jobs,
        ReplayClock(outside),
    ).execute(outside)

    assert fills == []
    assert monitored == []
    portfolio.capture_buy_fills.assert_not_called()
    portfolio.monitor_positions.assert_not_called()
    assert [finish[1] for finish in jobs.finishes] == ["missed", "success"]


def test_evaluation_and_postclose_use_cases_coordinate_only_their_collaborators(settings):
    repository = Mock()
    repository.latest_signal_run.return_value = None
    repository.previous_trading_day.return_value = None
    repository.get_risk_events.return_value = pd.DataFrame()
    repository.get_open_positions.return_value = pd.DataFrame()
    repository.get_cash.return_value = settings.initial_cash
    evaluation = Mock()
    jobs = FakeJobs()
    clock = ReplayClock(AT)
    morning = EvaluateMorningUseCase(repository, evaluation, jobs, clock)
    previous = EvaluatePreviousPrecloseUseCase(repository, evaluation, clock)
    news = Mock()
    news.sync.return_value = NewsSyncResult(True, True, 0, 0, 0, 0, [])
    report = Mock()
    report.generate.return_value = "# report"

    assert morning.execute(AT) == []
    assert previous.execute(AT) == []
    content = RunPostcloseUseCase(
        settings,
        repository,
        news,
        morning,
        previous,
        lambda: report,
        jobs,
        clock,
    ).execute(AT)

    assert content == "# report"
    report.generate.assert_called_once()
    evaluation.evaluate.assert_not_called()


def test_recover_expired_jobs_uses_injected_clock_when_time_is_omitted():
    repository = Mock()
    repository.recover_expired_jobs.return_value = ["expired-job"]

    recovered = RecoverExpiredJobsUseCase(repository, ReplayClock(AT)).execute()

    assert recovered == ["expired-job"]
    repository.recover_expired_jobs.assert_called_once_with(AT)
