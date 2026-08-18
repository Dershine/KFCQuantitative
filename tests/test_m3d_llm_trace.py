from __future__ import annotations

import hashlib
from datetime import datetime
from types import SimpleNamespace

import duckdb
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import MIGRATIONS, Database
from kfcquant.migrations import Migration, MigrationRunner
from kfcquant.models import LLMCallStatus, LLMCallTrace, NewsDocument, RiskExtractionResult, SourceTier
from kfcquant.providers.qwen import (
    RISK_EXTRACTION_PROMPT_VERSION,
    QwenLLMProvider,
    _safe_failure_message,
)
from kfcquant.services.news import NewsService


class EmptyProvider:
    def fetch_official_documents(self, start, end):
        return []

    def fetch_mainstream_documents(self, start, end):
        return []


class NoDownload:
    def load_text(self, url):
        raise AssertionError("download should not be called")


class CapturingCompletions:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.request: dict[str, object] | None = None

    def create(self, **kwargs):
        self.request = kwargs
        if self.failure is not None:
            raise self.failure
        content = (
            '{"events":[{"event_type":"regulatory_investigation","direction":"negative",'
            '"severity":"critical","confidence":0.99,"evidence":"立案调查"}]}'
        )
        return SimpleNamespace(
            model="fixture-model-resolved",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


class MalformedCompletions:
    def create(self, **kwargs):
        return SimpleNamespace(
            model="fixture-model-resolved",
            choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))],
        )


def _document(at: datetime) -> NewsDocument:
    title = "关于收到立案调查通知书的公告"
    return NewsDocument(
        document_id="doc-traced",
        ts_code="600000.SH",
        title=title,
        content="公司收到监管机构立案调查通知。",
        published_at=at,
        source="fixture",
        source_tier=SourceTier.OFFICIAL,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        fetched_at=at,
    )


def _provider(settings, completions: CapturingCompletions) -> QwenLLMProvider:
    provider = QwenLLMProvider.__new__(QwenLLMProvider)
    provider.settings = settings
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider


def test_risk_extraction_returns_versioned_hash_only_trace(settings):
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    completions = CapturingCompletions()
    result = _provider(settings, completions).extract_risk_events(_document(at))

    assert isinstance(result, RiskExtractionResult)
    assert result.trace is not None
    assert result.trace.status == LLMCallStatus.SUCCESS
    assert result.trace.prompt_version == RISK_EXTRACTION_PROMPT_VERSION
    assert result.trace.requested_model == settings.llm_extract_model
    assert result.trace.response_model == "fixture-model-resolved"
    assert len(result.trace.prompt_sha256) == 64
    assert len(result.trace.input_sha256) == 64
    assert len(result.trace.response_sha256 or "") == 64
    assert result.trace.duration_ms >= 0
    assert result.events[0].llm_call_id == result.trace.call_id
    serialized = result.trace.model_dump_json()
    assert "公司收到监管机构" not in serialized
    if settings.llm_api_key:
        assert settings.llm_api_key not in serialized


def test_failed_llm_call_is_traced_and_document_stays_failed_closed(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    document = _document(at)
    database.save_news_documents([document])
    traced_settings = settings.model_copy(update={"llm_api_key": "must-not-be-persisted"})
    completions = CapturingCompletions(failure=TimeoutError("provider timed out: must-not-be-persisted"))
    service = NewsService(database, EmptyProvider(), _provider(traced_settings, completions), NoDownload())

    assert service.process_pending() == (0, 1)
    traces = database.table("llm_call_traces")
    stored_document = database.table("news_documents").iloc[0]
    assert traces.iloc[0]["status"] == LLMCallStatus.FAILED.value
    assert traces.iloc[0]["error_type"] == "TimeoutError"
    assert "must-not-be-persisted" not in traces.iloc[0]["error_message"]
    assert traces.iloc[0]["document_id"] == document.document_id
    assert stored_document["processing_status"] == "failed"
    assert database.table("risk_events").empty


def test_successful_trace_and_event_link_are_persisted_together(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    document = _document(at)
    database.save_news_documents([document])
    service = NewsService(database, EmptyProvider(), _provider(settings, CapturingCompletions()), NoDownload())

    assert service.process_pending() == (1, 0)
    event = database.table("risk_events").iloc[0]
    trace = database.llm_trace_for_risk_event(str(event["event_id"]))
    assert trace is not None
    assert trace["document_id"] == document.document_id
    assert trace["status"] == LLMCallStatus.SUCCESS.value
    assert database.table("news_documents").iloc[0]["processing_status"] == "processed"


def test_trace_event_and_document_success_roll_back_as_one_unit(settings, monkeypatch):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    document = _document(at)
    database.save_news_documents([document])
    original = Database._insert_risk_events

    def fail_after_trace(connection, events):
        original(connection, events)
        raise RuntimeError("injected event persistence failure")

    monkeypatch.setattr(Database, "_insert_risk_events", staticmethod(fail_after_trace))
    service = NewsService(database, EmptyProvider(), _provider(settings, CapturingCompletions()), NoDownload())
    assert service.process_pending() == (0, 1)
    assert database.table("llm_call_traces").empty
    assert database.table("risk_events").empty
    assert database.table("news_documents").iloc[0]["processing_status"] == "failed"
    monkeypatch.setattr(Database, "_insert_risk_events", staticmethod(original))


def test_invalid_llm_response_keeps_response_hash_in_failed_trace(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    database.save_news_documents([_document(at)])
    service = NewsService(database, EmptyProvider(), _provider(settings, MalformedCompletions()), NoDownload())

    assert service.process_pending() == (0, 1)
    trace = database.table("llm_call_traces").iloc[0]
    assert trace["error_type"] == "JSONDecodeError"
    assert len(trace["response_sha256"]) == 64


def test_llm_trace_migration_rolls_back_and_recovers(tmp_path):
    llm_migration = next(migration for migration in MIGRATIONS if migration.version == 8)
    broken = (
        *MIGRATIONS[:7],
        Migration(8, "broken_llm_trace", (*llm_migration.statements, "BAD SQL")),
    )
    path = tmp_path / "llm-trace-migration.duckdb"
    with duckdb.connect(str(path)) as connection:
        runner = MigrationRunner(connection)
        with pytest.raises(duckdb.Error):
            runner.apply(broken)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 7
        assert not connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='llm_call_traces'"
        ).fetchone()

        runner.apply(MIGRATIONS)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)
        assert connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='llm_call_traces'"
        ).fetchone()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("secret"), "LLM provider timed out"),
        (ValueError("secret"), "LLM response violated the risk extraction contract"),
        (RuntimeError("secret"), "LLM risk extraction call failed"),
    ],
)
def test_failure_messages_are_classified_without_persisting_exception_text(error, expected):
    assert _safe_failure_message(error) == expected


def test_llm_trace_rejects_incomplete_or_inconsistent_outcomes():
    base = {
        "document_id": "doc",
        "provider": "fixture",
        "prompt_version": "v1",
        "prompt_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "requested_model": "fixture",
        "started_at": datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ),
        "duration_ms": 1,
    }
    with pytest.raises(ValueError, match="response model and hash"):
        LLMCallTrace(**base, status=LLMCallStatus.SUCCESS)
    with pytest.raises(ValueError, match="cannot contain failure"):
        LLMCallTrace(
            **base,
            status=LLMCallStatus.SUCCESS,
            response_model="fixture",
            response_sha256="c" * 64,
            error_type="unexpected",
        )
    with pytest.raises(ValueError, match="requires error_type"):
        LLMCallTrace(**base, status=LLMCallStatus.FAILED)
    with pytest.raises(ValueError, match="include timezone"):
        LLMCallTrace(
            **{**base, "started_at": base["started_at"].replace(tzinfo=None)},
            status=LLMCallStatus.FAILED,
            error_type="TimeoutError",
        )
