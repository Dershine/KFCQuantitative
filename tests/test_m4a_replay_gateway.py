from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pandas as pd
import pytest

from kfcquant.clock import ReplayClock
from kfcquant.config import SHANGHAI_TZ
from kfcquant.models import ResearchRunState, RunStatus, SignalKind, SignalRun
from kfcquant.point_in_time import PointInTimeDataGateway
from kfcquant.replay import ReplayDataGateway, ReplayInputViolation
from kfcquant.run_manifest import (
    ResearchRunManifest,
    RunInputKind,
    RunInputSnapshot,
    RunInputSnapshotStore,
    candidate_result_sha256,
)
from tests.conftest import make_daily, make_quotes, make_securities, strategy_attribution


def _inputs(at: datetime, signal_kind: SignalKind) -> dict[str, object]:
    codes = ["600000.SH", "000001.SZ"]
    values: dict[str, object] = {
        "run_id": f"replay-{signal_kind.value}",
        "signal_kind": signal_kind,
        "as_of": at,
        "information_cutoff": at,
        "securities": make_securities([(code, code) for code in codes]),
        "bars": make_daily(codes, at, days=3),
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
        "unprocessed_official_codes": frozenset({codes[0]}),
        "captured_at": at,
    }
    if signal_kind == SignalKind.PRECLOSE_ENTRY:
        values.update(
            {
                "quotes": make_quotes(codes, at).assign(source="fixture-live"),
                "previous_signal_codes": frozenset({codes[1]}),
                "previous_signal_as_of": at.replace(hour=8, minute=30),
            }
        )
    return values


def _manifest(
    at: datetime,
    signal_kind: SignalKind,
    snapshots: tuple[RunInputSnapshot, ...],
) -> ResearchRunManifest:
    run = SignalRun(
        **strategy_attribution(f"fixture-{signal_kind.value}"),
        run_id=f"replay-{signal_kind.value}",
        as_of=at,
        information_cutoff=at,
        signal_kind=signal_kind,
        status=RunStatus.DEGRADED,
        lifecycle_state=ResearchRunState.PUBLISHED,
        data_fresh=False,
        official_news_healthy=False,
        mainstream_news_healthy=False,
        tradable=False,
    )
    return ResearchRunManifest.create(
        run,
        snapshots,
        candidate_result_sha256([]),
        source_sha="a" * 40,
        source_dirty=False,
        project_version="0.2.0",
        python_version="3.13.0",
        dependency_lock_sha256="b" * 64,
        created_at=at,
    )


def _captured(tmp_path, signal_kind: SignalKind = SignalKind.PRECLOSE_ENTRY):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    if signal_kind == SignalKind.MORNING_WATCHLIST:
        at = at.replace(hour=8, minute=30)
    store = RunInputSnapshotStore(tmp_path / "raw")
    captured = PointInTimeDataGateway(store, clock=ReplayClock(at)).build_context(
        **_inputs(at, signal_kind)
    )
    return at, store, captured, _manifest(at, signal_kind, captured.snapshots)


def _replace_snapshot_frame(
    store: RunInputSnapshotStore,
    manifest: ResearchRunManifest,
    kind: RunInputKind,
    frame: pd.DataFrame,
) -> ResearchRunManifest:
    original = next(snapshot for snapshot in manifest.input_snapshots if snapshot.dataset_kind == kind)
    replacement = store.capture(
        kind,
        original.schema_version,
        original.source,
        frame,
        captured_at=original.captured_at,
        information_cutoff=original.information_cutoff,
        ingestion_batch_ids=original.ingestion_batch_ids,
    )
    snapshots = tuple(
        replacement if snapshot.dataset_kind == kind else snapshot
        for snapshot in manifest.input_snapshots
    )
    return _manifest(manifest.information_cutoff, manifest.signal_kind, snapshots)


@pytest.mark.parametrize("signal_kind", list(SignalKind))
def test_replay_gateway_rebuilds_formal_strategy_context_without_writes(tmp_path, signal_kind):
    at, store, captured, manifest = _captured(tmp_path, signal_kind)
    paths = [store.resolve(snapshot) for snapshot in manifest.input_snapshots]
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}

    context = ReplayDataGateway(store, ReplayClock(at)).load_context(manifest)

    assert context.run_id == manifest.run_id
    assert context.signal_kind == signal_kind
    assert context.as_of == at
    assert context.information_cutoff == manifest.information_cutoff
    pd.testing.assert_frame_equal(context.securities, captured.context.securities)
    pd.testing.assert_frame_equal(context.bars, captured.context.bars)
    pd.testing.assert_frame_equal(context.quotes, captured.context.quotes)
    pd.testing.assert_frame_equal(context.risk_events, captured.context.risk_events)
    assert context.unprocessed_official_codes == captured.context.unprocessed_official_codes
    assert context.previous_signal_codes == captured.context.previous_signal_codes
    assert before == {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}


@pytest.mark.parametrize(
    ("kind", "change", "message"),
    [
        (RunInputKind.SECURITY, {"schema_version": "security-v0"}, "schema version"),
        (RunInputKind.DAILY_BAR, {"row_count": 999}, "row count"),
        (
            RunInputKind.RISK_EVENT,
            {"information_cutoff": datetime(2026, 8, 10, 14, 39, tzinfo=SHANGHAI_TZ)},
            "information cutoff",
        ),
    ],
)
def test_replay_gateway_fails_closed_on_manifest_snapshot_contract_mismatch(
    tmp_path, kind, change, message
):
    at, store, _captured_context, manifest = _captured(tmp_path)
    changed = tuple(
        snapshot.model_copy(update=change) if snapshot.dataset_kind == kind else snapshot
        for snapshot in manifest.input_snapshots
    )
    changed_manifest = _manifest(at, manifest.signal_kind, changed)

    with pytest.raises(ReplayInputViolation, match=message):
        ReplayDataGateway(store, ReplayClock(at)).load_context(changed_manifest)


def test_replay_gateway_rejects_missing_and_tampered_snapshot_files(tmp_path):
    at, store, _captured_context, manifest = _captured(tmp_path)
    gateway = ReplayDataGateway(store, ReplayClock(at))
    target = store.resolve(manifest.input_snapshots[0])
    original = target.read_bytes()

    target.unlink()
    with pytest.raises(ReplayInputViolation, match="missing"):
        gateway.load_context(manifest)
    target.write_bytes(original + b"tampered")
    with pytest.raises(ReplayInputViolation, match="hash mismatch"):
        gateway.load_context(manifest)


def test_replay_gateway_rejects_hash_valid_but_unreadable_parquet(tmp_path):
    at, store, _captured_context, manifest = _captured(tmp_path)
    security = next(
        snapshot for snapshot in manifest.input_snapshots if snapshot.dataset_kind == RunInputKind.SECURITY
    )
    content = b"not a parquet file"
    digest = hashlib.sha256(content).hexdigest()
    target = store.root / "run-inputs" / RunInputKind.SECURITY.value / f"{digest}.parquet"
    target.write_bytes(content)
    unreadable = security.model_copy(
        update={
            "snapshot_id": digest,
            "snapshot_path": target.relative_to(store.root).as_posix(),
            "content_sha256": digest,
        }
    )
    snapshots = tuple(
        unreadable if snapshot.dataset_kind == RunInputKind.SECURITY else snapshot
        for snapshot in manifest.input_snapshots
    )
    unreadable_manifest = _manifest(at, manifest.signal_kind, snapshots)

    with pytest.raises(ReplayInputViolation, match="cannot be read"):
        ReplayDataGateway(store, ReplayClock(at)).load_context(unreadable_manifest)


def test_replay_gateway_rejects_clock_drift_and_future_snapshot_data(tmp_path):
    at, store, _captured_context, manifest = _captured(tmp_path)
    with pytest.raises(ReplayInputViolation, match="ReplayClock.*information cutoff"):
        ReplayDataGateway(store, ReplayClock(at + timedelta(seconds=1))).load_context(manifest)

    quote = next(
        snapshot for snapshot in manifest.input_snapshots if snapshot.dataset_kind == RunInputKind.LIVE_QUOTE
    )
    future_quote = store.capture(
        RunInputKind.LIVE_QUOTE,
        quote.schema_version,
        quote.source,
        make_quotes(["600000.SH", "000001.SZ"], at + timedelta(seconds=1)).assign(
            source="fixture-live"
        ),
        captured_at=at,
        information_cutoff=at,
    )
    future_snapshots = tuple(
        future_quote if snapshot.dataset_kind == RunInputKind.LIVE_QUOTE else snapshot
        for snapshot in manifest.input_snapshots
    )
    future_manifest = _manifest(at, manifest.signal_kind, future_snapshots)

    with pytest.raises(ReplayInputViolation, match="live_quote.*information_cutoff"):
        ReplayDataGateway(store, ReplayClock(at)).load_context(future_manifest)


class _NaiveClock:
    def now(self):
        return datetime(2026, 8, 10, 14, 40)  # noqa: DTZ001 - deliberate invalid Clock fixture


def test_replay_gateway_rejects_naive_clock_and_unexpected_manifest_inputs(tmp_path):
    at, store, _captured_context, manifest = _captured(tmp_path)
    with pytest.raises(ReplayInputViolation, match="timezone-aware"):
        ReplayDataGateway(store, _NaiveClock()).load_context(manifest)

    morning_at, morning_store, morning_context, morning_manifest = _captured(
        tmp_path / "morning", SignalKind.MORNING_WATCHLIST
    )
    unexpected_quote = store.capture(
        RunInputKind.LIVE_QUOTE,
        "live-quote-v1",
        "fixture-live",
        make_quotes(["600000.SH"], at),
        captured_at=at,
        information_cutoff=at,
    ).model_copy(update={"information_cutoff": morning_at})
    unexpected_manifest = _manifest(
        morning_at,
        SignalKind.MORNING_WATCHLIST,
        (*morning_context.snapshots, unexpected_quote),
    )
    with pytest.raises(ReplayInputViolation, match="unexpected=.*live_quote"):
        ReplayDataGateway(morning_store, ReplayClock(morning_at)).load_context(unexpected_manifest)


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pd.DataFrame({"wrong": ["600000.SH"]}), "only the ts_code"),
        (pd.DataFrame({"ts_code": [None]}), "null ts_code"),
        (pd.DataFrame({"ts_code": ["600000.SH", "600000.SH"]}), "blank or duplicate"),
    ],
)
def test_replay_gateway_rejects_invalid_code_set_snapshots(tmp_path, frame, message):
    at, store, _captured_context, manifest = _captured(tmp_path)
    invalid_manifest = _replace_snapshot_frame(
        store,
        manifest,
        RunInputKind.UNPROCESSED_OFFICIAL_CODE,
        frame,
    )

    with pytest.raises(ReplayInputViolation, match=message):
        ReplayDataGateway(store, ReplayClock(at)).load_context(invalid_manifest)


@pytest.mark.parametrize(
    ("kind", "frame", "message"),
    [
        (RunInputKind.SECURITY, pd.DataFrame({"ts_code": ["600000.SH"]}), "market input schema"),
        (
            RunInputKind.RISK_EVENT,
            pd.DataFrame({"event_id": ["event"], "ts_code": ["600000.SH"]}),
            "missing required columns",
        ),
    ],
)
def test_replay_gateway_rejects_malformed_typed_input_frames(tmp_path, kind, frame, message):
    at, store, _captured_context, manifest = _captured(tmp_path)
    invalid_manifest = _replace_snapshot_frame(store, manifest, kind, frame)

    with pytest.raises(ReplayInputViolation, match=message):
        ReplayDataGateway(store, ReplayClock(at)).load_context(invalid_manifest)
