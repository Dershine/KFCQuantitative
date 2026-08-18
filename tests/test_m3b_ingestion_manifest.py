from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.ingestion import (
    IngestionSnapshotStore,
    SnapshotIntegrityError,
    resolve_provider_identity,
)
from kfcquant.market_data import (
    DAILY_BAR_SCHEMA,
    LIVE_QUOTE_SCHEMA,
    SECURITY_SCHEMA,
    LogicalType,
    MarketColumn,
    MarketTableSchema,
)
from kfcquant.providers.akshare_live import AkShareLiveQuoteProvider
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLLM, FakeMarket, FakeRangeMarket


class MutableLiveProvider:
    source_name = "configured-live-default"

    def __init__(self, quotes: pd.DataFrame):
        self.quotes = quotes

    def fetch_quotes(self, ts_codes=None):
        frame = self.quotes.copy()
        if ts_codes:
            frame = frame[frame["ts_code"].isin(set(ts_codes))]
        return frame

    def fetch_intraday_bars(self, ts_code, start, end, frequency_minutes=5):
        return []


class EmptySinaFallbackClient:
    def stock_zh_a_spot_em(self):
        raise RuntimeError("eastmoney unavailable")

    def stock_zh_a_spot(self):
        return pd.DataFrame(
            columns=["代码", "最新价", "今开", "最高", "最低", "昨收", "成交量", "成交额", "时间戳"]
        )


def test_snapshot_store_creates_immutable_verifiable_manifest_for_nonempty_and_empty_batches(tmp_path):
    store = IngestionSnapshotStore(tmp_path / "raw")
    collected_at = datetime(2026, 8, 10, 16, 5, tzinfo=SHANGHAI_TZ)
    daily = DAILY_BAR_SCHEMA.validate(
        make_daily(["600000.SH"], collected_at, days=2)
    )

    first = store.capture(daily, "fixture-market", collected_at, "job-1")
    second = store.capture(daily, "fixture-market", collected_at, "job-2")
    empty = store.capture(
        DAILY_BAR_SCHEMA.validate(pd.DataFrame()),
        "fixture-market",
        collected_at,
        "job-3",
    )

    assert first.batch_id != second.batch_id
    assert first.snapshot_path != second.snapshot_path
    assert first.dataset_kind.value == "daily_bar"
    assert first.schema_version == "daily-bar-v1"
    assert first.provider == "fixture-market"
    assert first.collected_at == collected_at
    assert first.row_count == 2
    assert first.quality_report["validation_passed"] is True
    assert first.quality_report["unique_key"] == ["ts_code", "trade_date"]
    assert first.quality_report["null_counts"]["up_limit"] == 0
    assert empty.row_count == 0
    assert store.verify(first)
    assert store.verify(empty)

    snapshot = store.resolve(first)
    assert not first.snapshot_path.is_absolute()
    assert snapshot.read_bytes()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == first.content_sha256
    snapshot.write_bytes(snapshot.read_bytes() + b"tampered")
    with pytest.raises(SnapshotIntegrityError, match="hash mismatch"):
        store.verify(first)
    store.resolve(second).unlink()
    with pytest.raises(SnapshotIntegrityError, match="missing"):
        store.verify(second)


def test_ingestion_manifest_rejects_invalid_identity_time_hash_report_and_schema(tmp_path):
    store = IngestionSnapshotStore(tmp_path / "raw")
    collected_at = datetime(2026, 8, 10, 16, 5, tzinfo=SHANGHAI_TZ)
    validated = DAILY_BAR_SCHEMA.validate(pd.DataFrame())
    manifest = store.capture(validated, "fixture-market", collected_at)

    invalid_cases = [
        ({"batch_id": ""}, "batch_id"),
        ({"provider": "bad/provider"}, "provider identity"),
        ({"collected_at": collected_at.replace(tzinfo=None)}, "timezone-aware"),
        ({"snapshot_path": tmp_path / "absolute.parquet"}, "safe relative"),
        ({"content_sha256": "not-a-hash"}, "SHA-256"),
        ({"row_count": -1}, "non-negative"),
        (
            {
                "quality_report_json": json.dumps(
                    {**manifest.quality_report, "validation_passed": False}, sort_keys=True
                )
            },
            "validated batch",
        ),
    ]
    for changes, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            replace(manifest, **changes)

    with pytest.raises(ValueError, match="invalid provider identity"):
        store.capture(validated, "bad/provider", collected_at)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.capture(validated, "fixture-market", collected_at.replace(tzinfo=None))

    unsupported = MarketTableSchema(
        name="unsupported",
        version="unsupported-v1",
        fields=(MarketColumn("value", LogicalType.STRING),),
        unique_key=("value",),
    ).validate(pd.DataFrame({"value": ["ok"]}))
    with pytest.raises(ValueError, match="unsupported ingestion schema"):
        store.capture(unsupported, "fixture-market", collected_at)


def test_provider_identity_uses_actual_quote_source_and_rejects_ambiguous_or_missing_metadata():
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    quote_frame = make_quotes(["600000.SH"], at).assign(source="akshare-sina")

    assert resolve_provider_identity(SimpleNamespace(source_name="tushare")) == "tushare"
    assert (
        resolve_provider_identity(
            SimpleNamespace(source_name="akshare-eastmoney"),
            LIVE_QUOTE_SCHEMA.validate(quote_frame),
        )
        == "akshare-sina"
    )

    mixed = pd.concat(
        [quote_frame, make_quotes(["000001.SZ"], at).assign(source="akshare-eastmoney")],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="multiple provider sources"):
        resolve_provider_identity(
            SimpleNamespace(source_name="akshare"),
            LIVE_QUOTE_SCHEMA.validate(mixed),
        )
    with pytest.raises(ValueError, match="source_name"):
        resolve_provider_identity(object())
    with pytest.raises(ValueError, match="invalid provider identity"):
        resolve_provider_identity(SimpleNamespace(source_name="bad/provider"))


def test_empty_live_batch_preserves_the_actual_fallback_provider_identity():
    provider = AkShareLiveQuoteProvider(EmptySinaFallbackClient())

    quotes = provider.fetch_quotes()

    assert quotes.empty
    assert provider.source_name == "akshare-sina"
    assert resolve_provider_identity(provider, LIVE_QUOTE_SCHEMA.validate(quotes)) == "akshare-sina"


def test_market_batch_and_manifest_are_atomic_and_recoverable(settings, monkeypatch):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    store = IngestionSnapshotStore(settings.raw_data_dir)
    collected_at = datetime(2026, 8, 10, 16, 5, tzinfo=SHANGHAI_TZ)
    validated = SECURITY_SCHEMA.validate(make_securities([("600000.SH", "公司")]))
    manifest = store.capture(validated, "fixture-market", collected_at, "job-atomic")
    original = Database._write_ingestion_manifest

    def fail_after_data(connection, incoming):
        raise RuntimeError(f"injected manifest failure: {incoming.batch_id}")

    monkeypatch.setattr(Database, "_write_ingestion_manifest", staticmethod(fail_after_data))
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        database.ingest_market_batch(validated.frame, manifest)

    assert database.get_securities().empty
    assert database.table("ingestion_manifests").empty
    assert store.verify(manifest)

    monkeypatch.setattr(Database, "_write_ingestion_manifest", staticmethod(original))
    database.ingest_market_batch(validated.frame, manifest)
    persisted = database.get_ingestion_manifest(manifest.batch_id)

    database.ingest_market_batch(validated.frame, manifest)
    with pytest.raises(ValueError, match="manifest collision"):
        database.ingest_market_batch(
            validated.frame,
            replace(manifest, collected_at=manifest.collected_at + timedelta(seconds=1)),
        )

    assert database.get_securities()["ts_code"].tolist() == ["600000.SH"]
    assert len(database.table("ingestion_manifests")) == 1
    assert persisted is not None
    assert persisted["provider"] == "fixture-market"
    assert persisted["quality_report"] == manifest.quality_report
    assert persisted["content_sha256"] == manifest.content_sha256


def test_sync_eod_persists_queryable_manifests_for_every_normalized_batch(settings):
    collected_at = datetime(2026, 8, 10, 16, 30, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    market = FakeRangeMarket(
        make_securities([(code, "公司")]),
        make_daily([code], collected_at, days=3),
    )
    workflow = Workflow(
        settings,
        database=database,
        market_provider=market,
        live_provider=MutableLiveProvider(pd.DataFrame()),
        news_provider=FakeMarket(),
        llm_provider=FakeLLM(),
    )

    result = workflow.sync_eod(date(2026, 8, 5), date(2026, 8, 10))
    manifests = database.table("ingestion_manifests", limit=10)

    assert result["ingestion_batches"] == 3
    assert set(manifests["dataset_kind"]) == {"security", "trade_calendar", "daily_bar"}
    assert set(manifests["provider"]) == {"fixture-range"}
    assert set(manifests["job_run_id"]) == {database.latest_job("sync-eod")["job_run_id"]}
    assert all(
        IngestionSnapshotStore(settings.raw_data_dir).verify(
            database.get_ingestion_manifest(batch_id)["manifest"]
        )
        for batch_id in manifests["batch_id"]
    )


def test_preclose_and_fill_use_actual_live_batch_source_without_hardcoding(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ", "002001.SZ", "603001.SH", "001001.SZ", "605001.SH"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": at.date() - timedelta(days=3)}]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    live = MutableLiveProvider(make_quotes(codes, at).assign(source="fixture-live-primary"))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=live,
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(as_of=at)
    assert run.tradable
    live.quotes = make_quotes(codes, at + timedelta(minutes=5)).assign(
        source="fixture-live-fallback",
        volume=lambda frame: frame["volume"] + 100_000,
        amount=lambda frame: frame["amount"] + 2_000_000,
    )
    fills = workflow.capture_fill(at + timedelta(minutes=5))

    manifests = database.table("ingestion_manifests", limit=10)
    assert fills
    assert manifests["dataset_kind"].tolist().count("live_quote") == 2
    assert set(manifests["provider"]) == {"fixture-live-primary", "fixture-live-fallback"}
    assert not any("akshare" in path for path in manifests["snapshot_path"].astype(str))
