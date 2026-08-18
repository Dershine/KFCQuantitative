from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import MIGRATIONS, Database
from kfcquant.migrations import Migration, MigrationRunner
from kfcquant.models import NewsDocument, SourceTier
from kfcquant.providers.qwen import RISK_EXTRACTION_PROMPT_VERSION, QwenLLMProvider
from kfcquant.run_manifest import RunInputKind
from kfcquant.services.news import NewsService
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeMarket


class StaticNewsProvider:
    def __init__(self, documents: list[NewsDocument] | None = None):
        self.documents = documents or []

    def fetch_official_documents(self, start, end):
        return list(self.documents)

    def fetch_mainstream_documents(self, start, end):
        return []


class NoDownload:
    def load_text(self, url):
        raise AssertionError("download should not be called")


class GroundedCompletions:
    def create(self, **kwargs):
        content = (
            '{"events":[{"event_type":"regulatory_investigation","direction":"negative",'
            '"severity":"critical","confidence":0.99,"evidence":"立案调查"}]}'
        )
        return SimpleNamespace(
            model="fixture-model-resolved",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def _llm(settings) -> QwenLLMProvider:
    provider = QwenLLMProvider.__new__(QwenLLMProvider)
    provider.settings = settings
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=GroundedCompletions()))
    return provider


def _multi_document(at: datetime, title: str) -> NewsDocument:
    return NewsDocument(
        document_id="doc-multi",
        title=title,
        content="甲公司与乙公司均收到立案调查通知。",
        published_at=at,
        source="fixture",
        source_tier=SourceTier.OFFICIAL,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        fetched_at=at,
    )


def test_deterministic_mapping_persists_all_document_entities_with_relevance(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_securities(make_securities([("600000.SH", "甲公司"), ("000001.SZ", "乙公司")]))
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    document = _multi_document(at, "甲公司与乙公司联合举行说明会")
    service = NewsService(database, StaticNewsProvider([document]), None, NoDownload())

    result = service.sync(at - timedelta(minutes=1), at + timedelta(minutes=1))

    assert result.inserted_documents == 1
    entities = database.document_entities(document.document_id)
    assert entities[["ts_code", "relevance", "association_source"]].to_dict("records") == [
        {"ts_code": "000001.SZ", "relevance": 1.0, "association_source": "exact_title"},
        {"ts_code": "600000.SH", "relevance": 1.0, "association_source": "exact_title"},
    ]
    assert database.table("news_documents").iloc[0]["ts_code"] is None

    content_document = NewsDocument(
        document_id="doc-content-match",
        title="联合说明会",
        content="甲公司与乙公司共同出席。",
        published_at=at,
        source="fixture",
        source_tier=SourceTier.MAINSTREAM,
        content_hash="content-match-hash",
        fetched_at=at,
    )
    service._map_entities([content_document])
    assert database.save_news_documents([content_document]) == 1
    assert set(database.document_entities(content_document.document_id)["relevance"]) == {0.8}


def test_multi_entity_official_failure_blocks_every_affected_security(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_securities(make_securities([("600000.SH", "甲公司"), ("000001.SZ", "乙公司")]))
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    service = NewsService(
        database,
        StaticNewsProvider([_multi_document(at, "甲公司与乙公司收到立案调查通知")]),
        None,
        NoDownload(),
    )

    result = service.sync(at - timedelta(minutes=1), at + timedelta(minutes=1))

    assert result.failed_documents == 1
    assert database.unprocessed_official_codes(at.replace(hour=0), at) == {"600000.SH", "000001.SZ"}


def test_multi_entity_event_expands_by_security_and_preserves_trace_lineage(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    codes = ["600000.SH", "000001.SZ", "603001.SH"]
    database.upsert_securities(
        make_securities([("600000.SH", "甲公司"), ("000001.SZ", "乙公司"), ("603001.SH", "丙公司")])
    )
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    document = _multi_document(at - timedelta(minutes=10), "甲公司与乙公司收到立案调查通知")
    news_service = NewsService(database, StaticNewsProvider([document]), _llm(settings), NoDownload())
    news_result = news_service.sync(at - timedelta(days=1), at)
    assert (news_result.processed_documents, news_result.failed_documents) == (1, 0)
    events = database.get_risk_events(at - timedelta(days=1), at)
    assert events["event_id"].nunique() == 1
    assert set(events["ts_code"]) == {"600000.SH", "000001.SZ"}
    assert set(events["entity_relevance"]) == {1.0}

    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}]
        )
    )
    database.upsert_daily_bars(make_daily(codes, at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        news_provider=StaticNewsProvider(),
        llm_provider=_llm(settings),
    )
    run = workflow.run_preclose(at)
    manifest = database.get_run_manifest(run.run_id)["manifest"]
    risk_snapshot = next(item for item in manifest.input_snapshots if item.dataset_kind == RunInputKind.RISK_EVENT)
    snapshotted_events = pd.read_parquet(settings.raw_data_dir / risk_snapshot.snapshot_path)
    lineage = database.llm_lineage_for_risk_events(snapshotted_events["event_id"].astype(str).unique().tolist())
    assert set(lineage["event_id"]) == set(snapshotted_events["event_id"])
    assert set(lineage["prompt_version"]) == {RISK_EXTRACTION_PROMPT_VERSION}
    assert lineage["input_sha256"].str.fullmatch(r"[0-9a-f]{64}").all()
    assert set(database.get_candidates(run.run_id).query("blocked")["ts_code"]) == {
        "600000.SH",
        "000001.SZ",
    }


def test_multi_entity_migration_rolls_back_and_recovers(tmp_path):
    entity_migration = next(migration for migration in MIGRATIONS if migration.version == 9)
    broken = (
        *MIGRATIONS[:8],
        Migration(9, "broken_multi_entity", (*entity_migration.statements, "BAD SQL")),
    )
    path = tmp_path / "multi-entity-migration.duckdb"
    with duckdb.connect(str(path)) as connection:
        runner = MigrationRunner(connection)
        with pytest.raises(duckdb.Error):
            runner.apply(broken)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 8
        assert not connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='document_entities'"
        ).fetchone()

        runner.apply(MIGRATIONS)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 9
        assert connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='risk_event_entities'"
        ).fetchone()


def test_previous_release_positional_writers_remain_readable(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    with duckdb.connect(str(settings.database_path)) as connection:
        connection.execute(
            """INSERT INTO news_documents VALUES (
               'legacy-doc', '600000.SH', 'legacy', NULL, ?, 'legacy', 'official', NULL,
               'legacy-hash', ?, 'failed', 'legacy failure'
               )""",
            [at, at],
        )
        connection.execute(
            """INSERT INTO risk_events VALUES (
               'legacy-event', 'legacy-doc', '600000.SH', 'other_risk', 'negative', 'medium',
               0.5, false, 'legacy evidence', NULL, ?, ?, 'legacy-model'
               )""",
            [at, at],
        )

    events = database.get_risk_events(at - timedelta(minutes=1), at + timedelta(minutes=1))
    assert events[["event_id", "ts_code", "entity_association_source"]].to_dict("records") == [
        {
            "event_id": "legacy-event",
            "ts_code": "600000.SH",
            "entity_association_source": "legacy",
        }
    ]
    assert database.unprocessed_official_codes(at.replace(hour=0), at) == {"600000.SH"}


def test_v8_upgrade_backfills_legacy_single_entity_relations(tmp_path):
    path = tmp_path / "v8-intelligence.duckdb"
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    with duckdb.connect(str(path)) as connection:
        MigrationRunner(connection).apply(MIGRATIONS[:8])
        connection.execute(
            """INSERT INTO news_documents VALUES (
               'v8-doc', '600000.SH', 'legacy', NULL, ?, 'legacy', 'official', NULL,
               'v8-hash', ?, 'processed', NULL
               )""",
            [at, at],
        )
        connection.execute(
            """INSERT INTO risk_events VALUES (
               'v8-event', 'v8-doc', '600000.SH', 'other_risk', 'negative', 'medium',
               0.5, false, 'legacy evidence', NULL, ?, ?, 'legacy-model'
               )""",
            [at, at],
        )

    database = Database(path)
    database.initialize()

    assert database.table("document_entities")[["document_id", "ts_code", "association_source"]].to_dict(
        "records"
    ) == [{"document_id": "v8-doc", "ts_code": "600000.SH", "association_source": "legacy"}]
    assert database.table("risk_event_entities")[["event_id", "ts_code", "association_source"]].to_dict(
        "records"
    ) == [{"event_id": "v8-event", "ts_code": "600000.SH", "association_source": "legacy"}]


def test_document_and_entity_persistence_roll_back_together(settings, monkeypatch):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_securities(make_securities([("600000.SH", "甲公司"), ("000001.SZ", "乙公司")]))
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    document = _multi_document(at, "甲公司与乙公司联合举行说明会")
    service = NewsService(database, StaticNewsProvider(), None, NoDownload())
    service._map_entities([document])
    original = Database._insert_document_entities

    def fail_after_entities(connection, candidate):
        original(connection, candidate)
        raise RuntimeError("injected entity persistence failure")

    monkeypatch.setattr(Database, "_insert_document_entities", staticmethod(fail_after_entities))
    with pytest.raises(RuntimeError, match="injected entity"):
        database.save_news_documents([document])
    assert database.table("news_documents").empty
    assert database.table("document_entities").empty
