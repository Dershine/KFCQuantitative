from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import pandas as pd
import pytest
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from kfcquant.application.use_cases import JobController
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import NewsDocument, SourceTier
from kfcquant.observability import (
    AlertCode,
    MemoryObservabilitySink,
    MetricName,
    Observability,
    configure_observability,
    observe_provider,
)
from kfcquant.runtime import observe_worker_heartbeat
from kfcquant.services.news import NewsService
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def now(self) -> datetime:
        return self.current


def _records(sink: MemoryObservabilitySink, record_type: str) -> list[dict[str, object]]:
    return [record for record in sink.records if record["record_type"] == record_type]


def test_structured_event_carries_correlation_fields_and_redacts_secrets():
    sink = MemoryObservabilitySink()
    observability = Observability((sink,), secret_values=("top-secret-token",))
    cutoff = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)

    with observability.context(
        job_run_id="job-1",
        signal_run_id="run-1",
        strategy_id="preclose-entry",
        strategy_version="preclose-v2",
        source_sha="a" * 40,
        provider="fixture-live",
        stage="collect-quotes",
        information_cutoff=cutoff,
    ):
        observability.event(
            "provider_call_failed",
            authorization="Bearer top-secret-token",
            url="https://example.test/api?token=top-secret-token&symbol=600000.SH",
            api_key="top-secret-token",
        )

    record = _records(sink, "log")[0]
    encoded = json.dumps(record, ensure_ascii=False, default=str)
    assert record["event"] == "provider_call_failed"
    assert record["job_run_id"] == "job-1"
    assert record["signal_run_id"] == "run-1"
    assert record["strategy_id"] == "preclose-entry"
    assert record["strategy_version"] == "preclose-v2"
    assert record["provider"] == "fixture-live"
    assert record["stage"] == "collect-quotes"
    assert record["information_cutoff"] == cutoff.isoformat()
    assert "top-secret-token" not in encoded
    assert "[REDACTED]" in encoded
    assert "symbol=600000.SH" in encoded


def test_provider_wrapper_records_duration_and_failure_without_changing_contract():
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))

    class Provider:
        source_name = "fixture-provider"

        def fetch_quotes(self):
            return ["quote"]

        def fetch_official_documents(self, start, end):
            raise TimeoutError("provider timed out")

    provider = observe_provider(Provider(), observability)
    assert provider.source_name == "fixture-provider"
    assert provider.fetch_quotes() == ["quote"]
    with pytest.raises(TimeoutError, match="provider timed out"):
        provider.fetch_official_documents(None, None)

    metrics = _records(sink, "metric")
    assert [item["metric"] for item in metrics].count(MetricName.PROVIDER_REQUEST_DURATION_SECONDS.value) == 2
    assert [item["metric"] for item in metrics].count(MetricName.PROVIDER_FAILURE_TOTAL.value) == 1
    failure = next(item for item in metrics if item["metric"] == MetricName.PROVIDER_FAILURE_TOTAL.value)
    assert failure["provider"] == "fixture-provider"
    assert failure["labels"]["operation"] == "fetch_official_documents"


def test_job_controller_emits_duration_status_and_preclose_failure_alert(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))
    started = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    clock = MutableClock(started)
    repository = SimpleNamespace(
        start_job=lambda *args, **kwargs: [],
        heartbeat_job=lambda *args, **kwargs: True,
        finish_job=lambda *args, **kwargs: None,
    )
    jobs = JobController(repository, settings, clock, observability)

    job_id = jobs.start("run-preclose", started)
    clock.current = started + timedelta(seconds=12)
    jobs.finish(job_id, "failed", "fixture failure")

    metrics = _records(sink, "metric")
    assert any(
        item["metric"] == MetricName.JOB_DURATION_SECONDS.value and item["value"] == 12
        for item in metrics
    )
    assert any(item["metric"] == MetricName.JOB_FAILED_TOTAL.value for item in metrics)
    alert = _records(sink, "alert")[0]
    assert alert["alert_code"] == AlertCode.PRECLOSE_RUN_FAILED.value
    assert alert["job_run_id"] == job_id
    assert alert["stage"] == "run-preclose"


def test_database_lock_wait_and_timeout_are_observable(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))
    lock_path = settings.runtime_dir / "observed-database.lock"
    database = Database(
        settings.database_path,
        settings.initial_cash,
        lock_timeout_seconds=0,
        lock_path=lock_path,
        observability=observability,
    )
    database.initialize()

    competing_lock = FileLock(lock_path, timeout=0)
    competing_lock.acquire()
    try:
        with pytest.raises(FileLockTimeout):
            with database.connect():
                pass
    finally:
        competing_lock.release()

    metrics = _records(sink, "metric")
    assert any(item["metric"] == MetricName.DATABASE_LOCK_WAIT_SECONDS.value for item in metrics)
    alerts = _records(sink, "alert")
    assert alerts[-1]["alert_code"] == AlertCode.DATABASE_LOCK_TIMEOUT.value


def test_stale_worker_heartbeat_emits_age_metric_and_deduplicated_alert(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,), alert_cooldown_seconds=900)
    now = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    (settings.runtime_dir / "worker-heartbeat.json").write_text(
        json.dumps({"at": (now - timedelta(minutes=10)).isoformat()}),
        encoding="utf-8",
    )

    first = observe_worker_heartbeat(settings, observability, now=now)
    second = observe_worker_heartbeat(settings, observability, now=now + timedelta(seconds=30))

    assert first["ok"] is False
    assert first["age_seconds"] == pytest.approx(600)
    assert second["ok"] is False
    assert any(
        item["metric"] == MetricName.WORKER_HEARTBEAT_AGE_SECONDS.value and item["value"] == pytest.approx(600)
        for item in _records(sink, "metric")
    )
    assert len(_records(sink, "alert")) == 1
    assert _records(sink, "alert")[0]["alert_code"] == AlertCode.WORKER_HEARTBEAT_STALE.value


def test_missing_heartbeat_and_sink_failure_are_visible_without_breaking_business(settings):
    sink = MemoryObservabilitySink()

    class FailingSink:
        def emit(self, record):
            raise OSError("fixture sink unavailable")

    observability = Observability((FailingSink(), sink))
    result = observe_worker_heartbeat(settings, observability)
    observability.event("business_continues")

    assert result == {"ok": False, "message": "heartbeat missing"}
    assert any(
        item.get("alert_code") == AlertCode.WORKER_HEARTBEAT_MISSING.value
        for item in sink.records
    )
    assert any(item.get("event") == AlertCode.ALERT_DELIVERY_FAILED.value for item in sink.records)
    assert any(item.get("event") == "business_continues" for item in sink.records)


def test_minimum_metric_contract_covers_every_m5c_operational_area():
    assert {item.value for item in MetricName} >= {
        "job_duration_seconds",
        "job_success_total",
        "job_failed_total",
        "job_missed_total",
        "provider_request_duration_seconds",
        "provider_failure_total",
        "quote_age_seconds",
        "latest_eod_lag_days",
        "official_news_pending",
        "llm_extraction_failure_total",
        "candidate_count",
        "order_rejection_total",
        "database_lock_wait_seconds",
        "worker_heartbeat_age_seconds",
    }


def test_configured_observability_writes_jsonl_and_delivers_deduplicated_webhook(settings):
    delivered: list[tuple[str, dict[str, object], dict[str, str]]] = []
    stream = StringIO()
    configured = settings.model_copy(
        update={
            "alert_webhook_url": "https://alerts.example.test/kfcquant",
            "alert_webhook_bearer_token": "webhook-secret",
        }
    )
    observability = configure_observability(
        configured,
        stream=stream,
        webhook_transport=lambda url, payload, headers: delivered.append((url, payload, headers)),
    )

    assert observability.alert(AlertCode.OFFICIAL_NEWS_UNHEALTHY, "official provider failed")
    assert not observability.alert(AlertCode.OFFICIAL_NEWS_UNHEALTHY, "duplicate")
    logging.getLogger("kfcquant.fixture").warning("Authorization: Bearer webhook-secret")

    stream_lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    stream_record = stream_lines[0]
    audit_record = json.loads(configured.alerts_path.read_text(encoding="utf-8").splitlines()[0])
    assert stream_record["alert_code"] == AlertCode.OFFICIAL_NEWS_UNHEALTHY.value
    assert audit_record["alert_code"] == AlertCode.OFFICIAL_NEWS_UNHEALTHY.value
    assert delivered[0][0] == configured.alert_webhook_url
    assert delivered[0][2]["Authorization"] == "Bearer webhook-secret"
    assert "webhook-secret" not in json.dumps(delivered[0][1])
    assert len(delivered) == 1
    assert stream_lines[1]["event"] == "application_log"
    assert "webhook-secret" not in json.dumps(stream_lines[1])


def test_news_failure_and_order_rejection_emit_operational_metrics(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))

    class NewsRepository:
        def get_securities(self):
            return make_securities([])

        def save_news_documents(self, documents):
            return len(documents)

        def pending_news_documents(self, limit=500):
            at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
            return [
                NewsDocument(
                    document_id="pending-official",
                    title="pending",
                    published_at=at,
                    source="fixture",
                    source_tier=SourceTier.OFFICIAL,
                    content_hash="pending-hash",
                    fetched_at=at,
                )
            ][:limit]

        def mark_document(self, document_id, status, error=None, content=None):
            return None

    class FailingNewsProvider:
        source_name = "fixture-news"

        def fetch_official_documents(self, start, end):
            raise TimeoutError("offline failure")

        def fetch_mainstream_documents(self, start, end):
            return []

    news = NewsService(
        NewsRepository(),
        FailingNewsProvider(),
        None,
        SimpleNamespace(load_text=lambda url: ""),
        observability,
        official_news_backlog_threshold=1,
    )
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    result = news.sync(at - timedelta(days=1), at)
    assert not result.official_healthy

    rejected: list[tuple[str, str]] = []
    order_at = at
    portfolio_repository = SimpleNamespace(
        proposed_orders=lambda run_id=None: pd.DataFrame(
            [
                {
                    "order_id": "order-1",
                    "run_id": "run-1",
                    "ts_code": "600000.SH",
                    "side": "buy",
                    "status": "proposed",
                    "created_at": order_at,
                    "target_value": 10_000.0,
                    "reason": "fixture",
                    "position_id": None,
                }
            ]
        ),
        get_candidates=lambda run_id, include_blocked=True: pd.DataFrame(
            [{"ts_code": "600000.SH", "rank": 1}]
        ),
        get_open_positions=lambda: pd.DataFrame(),
        reject_order=lambda order_id, reason: rejected.append((order_id, reason)),
    )
    portfolio = PortfolioService(
        portfolio_repository,
        settings,
        SimpleNamespace(),
        observability,
    )
    assert portfolio.capture_buy_fills("run-1", at, pd.DataFrame(columns=["ts_code"])) == []
    assert rejected[0][0] == "order-1"

    metrics = _records(sink, "metric")
    assert any(
        item["metric"] == MetricName.OFFICIAL_NEWS_PENDING.value and item["value"] == 1
        for item in metrics
    )
    assert any(item["metric"] == MetricName.ORDER_REJECTION_TOTAL.value for item in metrics)
    codes = {item["alert_code"] for item in _records(sink, "alert")}
    assert AlertCode.OFFICIAL_NEWS_UNHEALTHY.value in codes
    assert AlertCode.OFFICIAL_NEWS_BACKLOG.value in codes


def test_preclose_observability_correlates_job_run_strategy_provider_and_stage(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    database = Database(settings.database_path, settings.initial_cash, observability=observability)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        observability=observability,
    )

    run = workflow.run_preclose(as_of=at)

    records = sink.records
    started = next(item for item in records if item.get("event") == "job_started")
    candidate_metric = next(
        item for item in records if item.get("metric") == MetricName.CANDIDATE_COUNT.value
    )
    provider_metric = next(
        item for item in records if item.get("metric") == MetricName.PROVIDER_REQUEST_DURATION_SECONDS.value
    )
    committed = next(item for item in records if item.get("event") == "research_run_committed")
    assert candidate_metric["job_run_id"] == started["job_run_id"]
    assert provider_metric["job_run_id"] == started["job_run_id"]
    assert candidate_metric["signal_run_id"] == run.run_id
    assert candidate_metric["strategy_id"] == run.strategy_id
    assert candidate_metric["strategy_version"] == run.strategy_version
    assert candidate_metric["information_cutoff"] == run.information_cutoff.isoformat()
    assert provider_metric["provider"] == "fixture-live"
    assert committed["source_sha"]
    assert committed["stage"] == "research-run.publish"
    assert any(item.get("metric") == MetricName.JOB_SUCCESS_TOTAL.value for item in records)


def test_stale_preclose_data_alerts_and_remains_non_tradable(settings):
    sink = MemoryObservabilitySink()
    observability = Observability((sink,))
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash, observability=observability)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}]
        )
    )
    database.upsert_securities(make_securities([(code, code)]))
    database.upsert_daily_bars(make_daily([code], at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes([code], at - timedelta(minutes=2))),
        llm_provider=FakeLLM(),
        observability=observability,
    )

    run = workflow.run_preclose(as_of=at)

    assert not run.tradable
    assert database.table("paper_orders").empty
    assert any(
        item.get("alert_code") == AlertCode.QUOTE_DATA_STALE.value
        for item in sink.records
    )
