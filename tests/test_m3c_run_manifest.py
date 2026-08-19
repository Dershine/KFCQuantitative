from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest
from pydantic import ValidationError

from kfcquant.config import SHANGHAI_TZ
from kfcquant.ingestion import IngestionSnapshotStore
from kfcquant.market_data import DAILY_BAR_SCHEMA, LIVE_QUOTE_SCHEMA
from kfcquant.models import ResearchRunState, RunStatus, SignalKind, SignalRun
from kfcquant.point_in_time import PointInTimeDataGateway, PointInTimeViolation
from kfcquant.run_manifest import (
    ResearchRunManifest,
    RunInputKind,
    RunInputSnapshot,
    RunInputSnapshotStore,
    candidate_result_sha256,
)
from kfcquant.runtime import build_identity
from tests.conftest import make_daily, make_quotes, make_securities, strategy_attribution


def _gateway_inputs(at: datetime) -> dict[str, object]:
    codes = ["600000.SH"]
    return {
        "run_id": "point-in-time-run",
        "signal_kind": SignalKind.PRECLOSE_ENTRY,
        "as_of": at,
        "information_cutoff": at,
        "securities": make_securities([(codes[0], "公司")]),
        "bars": make_daily(codes, at, days=3),
        "quotes": make_quotes(codes, at).assign(source="fixture-live"),
        "risk_events": pd.DataFrame(
            [
                {
                    "event_id": "risk-before",
                    "ts_code": codes[0],
                    "published_at": at - timedelta(minutes=1),
                    "evidence": "原文证据",
                }
            ]
        ),
        "unprocessed_official_codes": frozenset(codes),
        "previous_signal_codes": frozenset(codes),
        "previous_signal_as_of": at.replace(hour=8, minute=30),
    }


def test_build_identity_prefers_release_sha_and_hashes_dependency_lock(monkeypatch):
    build_identity.cache_clear()
    monkeypatch.setenv("KFCQUANT_SOURCE_SHA", "c" * 40)
    monkeypatch.setenv("KFCQUANT_DEPENDENCY_LOCK_SHA256", "d" * 64)
    identity = build_identity()

    assert identity["source_sha"] == "c" * 40
    assert identity["source_dirty"] is False
    assert identity["dependency_lock_sha256"] == "d" * 64
    build_identity.cache_clear()


def test_build_identity_rejects_invalid_release_dependency_lock_hash(monkeypatch):
    build_identity.cache_clear()
    monkeypatch.setenv("KFCQUANT_SOURCE_SHA", "c" * 40)
    monkeypatch.setenv("KFCQUANT_DEPENDENCY_LOCK_SHA256", "unavailable")

    with pytest.raises(RuntimeError, match="KFCQUANT_DEPENDENCY_LOCK_SHA256"):
        build_identity()
    build_identity.cache_clear()


def test_point_in_time_gateway_captures_exact_deduplicated_inputs_and_upstream_batch(tmp_path):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    inputs = _gateway_inputs(at)
    quote_frame = inputs["quotes"]
    ingestion_store = IngestionSnapshotStore(tmp_path / "raw")
    quote_manifest = ingestion_store.capture(
        LIVE_QUOTE_SCHEMA.validate(quote_frame),
        "fixture-live",
        at,
        "quote-job",
    )
    snapshot_store = RunInputSnapshotStore(tmp_path / "raw")
    gateway = PointInTimeDataGateway(snapshot_store)

    first = gateway.build_context(**inputs, quote_ingestion_manifest=quote_manifest)
    second = gateway.build_context(**inputs, quote_ingestion_manifest=quote_manifest)

    assert first.context.information_cutoff == at
    assert {snapshot.dataset_kind for snapshot in first.snapshots} == {
        RunInputKind.SECURITY,
        RunInputKind.DAILY_BAR,
        RunInputKind.LIVE_QUOTE,
        RunInputKind.RISK_EVENT,
        RunInputKind.UNPROCESSED_OFFICIAL_CODE,
        RunInputKind.PREVIOUS_SIGNAL_CODE,
    }
    quote_snapshot = next(
        snapshot for snapshot in first.snapshots if snapshot.dataset_kind == RunInputKind.LIVE_QUOTE
    )
    assert quote_snapshot.ingestion_batch_ids == (quote_manifest.batch_id,)
    assert [snapshot.snapshot_id for snapshot in first.snapshots] == [
        snapshot.snapshot_id for snapshot in second.snapshots
    ]
    assert all(snapshot_store.verify(snapshot) for snapshot in first.snapshots)
    tampered = first.snapshots[0]
    path = snapshot_store.resolve(tampered)
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        snapshot_store.verify(tampered)
    with pytest.raises(RuntimeError, match="corrupted"):
        snapshot_store.capture(
            tampered.dataset_kind,
            tampered.schema_version,
            tampered.source,
            inputs["securities"],
            at,
            at,
        )
    missing = first.snapshots[1]
    snapshot_store.resolve(missing).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        snapshot_store.verify(missing)
    with pytest.raises(RuntimeError, match="escapes"):
        snapshot_store.resolve(missing.model_copy(update={"snapshot_path": "../escape.parquet"}))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"captured_at": datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ).replace(tzinfo=None)},
            "captured_at",
        ),
        (
            {
                "information_cutoff": datetime(
                    2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ
                ).replace(tzinfo=None)
            },
            "information_cutoff",
        ),
        ({"snapshot_path": "../escape.parquet"}, "safe relative"),
        ({"content_sha256": "invalid"}, "SHA-256"),
        ({"snapshot_id": "b" * 64}, "snapshot_id"),
        ({"schema_version": ""}, "must not be blank"),
        ({"ingestion_batch_ids": ("",)}, "must not be blank"),
        ({"ingestion_batch_ids": ("same", "same")}, "must be unique"),
    ],
)
def test_run_input_snapshot_rejects_ambiguous_or_unsafe_identity(changes, message):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    values = {
        "snapshot_id": "a" * 64,
        "dataset_kind": RunInputKind.SECURITY,
        "schema_version": "security-v1",
        "source": "fixture",
        "captured_at": at,
        "information_cutoff": at,
        "snapshot_path": "run-inputs/security/a.parquet",
        "content_sha256": "a" * 64,
        "row_count": 1,
    }
    with pytest.raises(ValidationError, match=message):
        RunInputSnapshot.model_validate({**values, **changes})


def test_run_manifest_hash_and_required_inputs_are_self_validating(tmp_path):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    gateway = PointInTimeDataGateway(RunInputSnapshotStore(tmp_path / "raw"))
    point_in_time = gateway.build_context(**_gateway_inputs(at))
    run = SignalRun(
        **strategy_attribution(),
        run_id="manifest-validation",
        as_of=at,
        information_cutoff=at,
        signal_kind=SignalKind.PRECLOSE_ENTRY,
        status=RunStatus.DEGRADED,
        lifecycle_state=ResearchRunState.PUBLISHED,
        data_fresh=False,
        official_news_healthy=False,
        mainstream_news_healthy=False,
        tradable=False,
    )
    manifest = ResearchRunManifest.create(
        run,
        point_in_time.snapshots,
        candidate_result_sha256([]),
        source_sha="a" * 40,
        source_dirty=True,
        project_version="0.2.0",
        python_version="3.13.0",
        dependency_lock_sha256="b" * 64,
        created_at=at,
    )

    assert ResearchRunManifest.model_validate_json(manifest.canonical_json) == manifest
    invalid_cases = [
        ({"information_cutoff": at.replace(tzinfo=None)}, "information_cutoff"),
        ({"created_at": at.replace(tzinfo=None)}, "created_at"),
        ({"source_sha": ""}, "source_sha"),
        ({"project_version": ""}, "project and Python"),
        ({"dependency_lock_sha256": ""}, "dependency lock"),
        (
            {"input_snapshots": [*manifest.input_snapshots, manifest.input_snapshots[0]]},
            "at most one",
        ),
        (
            {
                "input_snapshots": [
                    snapshot
                    for snapshot in manifest.input_snapshots
                    if snapshot.dataset_kind != RunInputKind.LIVE_QUOTE
                ]
            },
            "missing required",
        ),
        ({"result_sha256": "invalid"}, "result_sha256"),
        ({"manifest_sha256": "0" * 64}, "hash does not match"),
    ]
    for changes, message in invalid_cases:
        with pytest.raises(ValidationError, match=message):
            ResearchRunManifest.model_validate({**manifest.model_dump(), **changes})


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda values, at: values["securities"].assign(list_date=at.date() + timedelta(days=1)), "security"),
        (lambda values, at: values["bars"].assign(trade_date=at.date()), "daily_bar"),
        (lambda values, at: values["quotes"].assign(captured_at=at + timedelta(seconds=1)), "live_quote"),
        (
            lambda values, at: values["risk_events"].assign(published_at=at + timedelta(seconds=1)),
            "risk_event",
        ),
        (lambda values, at: at + timedelta(seconds=1), "previous_signal"),
    ],
)
def test_point_in_time_gateway_rejects_every_future_input_before_context_creation(
    tmp_path, mutation, message
):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    inputs = _gateway_inputs(at)
    if message == "previous_signal":
        inputs["previous_signal_as_of"] = mutation(inputs, at)
    else:
        key = {
            "security": "securities",
            "daily_bar": "bars",
            "live_quote": "quotes",
            "risk_event": "risk_events",
        }[message]
        inputs[key] = mutation(inputs, at)

    gateway = PointInTimeDataGateway(RunInputSnapshotStore(tmp_path / "raw"))
    with pytest.raises(PointInTimeViolation, match=message):
        gateway.build_context(**inputs)

    assert not (tmp_path / "raw" / "run-inputs").exists()


def test_point_in_time_gateway_rejects_invalid_times_and_ingestion_contracts(tmp_path):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    values = _gateway_inputs(at)
    gateway = PointInTimeDataGateway(RunInputSnapshotStore(tmp_path / "raw"))
    with pytest.raises(PointInTimeViolation, match="as_of.*timezone-aware"):
        gateway.build_context(**{**values, "as_of": at.replace(tzinfo=None)})
    with pytest.raises(PointInTimeViolation, match="cannot be after"):
        gateway.build_context(
            **{**values, "information_cutoff": at + timedelta(seconds=1)}
        )
    with pytest.raises(PointInTimeViolation, match="security.*invalid"):
        gateway.build_context(
            **{**values, "securities": values["securities"].assign(list_date=None)}
        )
    with pytest.raises(PointInTimeViolation, match="live_quote.*invalid"):
        gateway.build_context(
            **{**values, "quotes": values["quotes"].assign(captured_at=None)}
        )
    with pytest.raises(PointInTimeViolation, match="previous_signal_as_of.*timezone-aware"):
        gateway.build_context(
            **{**values, "previous_signal_as_of": at.replace(tzinfo=None)}
        )

    ingestion_store = IngestionSnapshotStore(tmp_path / "raw")
    quote = ingestion_store.capture(
        LIVE_QUOTE_SCHEMA.validate(values["quotes"]), "fixture-live", at, "quote-job"
    )
    daily = ingestion_store.capture(
        DAILY_BAR_SCHEMA.validate(values["bars"]), "fixture-market", at, "daily-job"
    )
    invalid_manifests = [
        (daily, "non-quote"),
        (
            replace(
                quote,
                schema_version="live-quote-v0",
                quality_report_json=quote.quality_report_json.replace(
                    '"schema_version":"live-quote-v1"', '"schema_version":"live-quote-v0"'
                ),
            ),
            "schema",
        ),
        (
            replace(
                quote,
                row_count=2,
                quality_report_json=quote.quality_report_json.replace('"row_count":1', '"row_count":2'),
            ),
            "row count",
        ),
        (replace(quote, provider="other-live"), "provider"),
    ]
    for manifest, message in invalid_manifests:
        with pytest.raises((PointInTimeViolation, ValueError), match=message):
            gateway.build_context(**values, quote_ingestion_manifest=manifest)
