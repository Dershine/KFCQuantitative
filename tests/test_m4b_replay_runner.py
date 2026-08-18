from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from kfcquant.clock import ReplayClock
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import ResearchRunState, RunStatus, SignalKind, SignalRun
from kfcquant.point_in_time import PointInTimeDataGateway, PointInTimeViolation
from kfcquant.replay import ReplayDataGateway, ReplayExecutionViolation, ReplayRunner
from kfcquant.run_manifest import ResearchRunManifest, RunInputSnapshotStore
from kfcquant.services.workflow import Workflow
from kfcquant.strategy import (
    StrategyContext,
    StrategyExecutionRunner,
    StrategyIdentity,
    StrategyParameterSnapshot,
    StrategyRegistry,
    StrategyResult,
    build_default_strategy_registry,
)
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def _inputs(at: datetime, signal_kind: SignalKind) -> dict[str, object]:
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    values: dict[str, object] = {
        "run_id": f"m4b-{signal_kind.value}",
        "signal_kind": signal_kind,
        "as_of": at,
        "information_cutoff": at,
        "securities": make_securities([(code, code) for code in codes]),
        "bars": make_daily(codes, at),
        "risk_events": pd.DataFrame(
            [
                {
                    "event_id": "supported-risk",
                    "ts_code": codes[0],
                    "published_at": at - timedelta(minutes=1),
                    "direction": "negative",
                    "severity": "high",
                    "confidence": 1.0,
                    "hard_block": True,
                    "event_type": "regulatory_investigation",
                    "evidence": "formal investigation",
                }
            ]
        ),
        "unprocessed_official_codes": frozenset(),
        "captured_at": at,
    }
    if signal_kind == SignalKind.PRECLOSE_ENTRY:
        values.update(
            {
                "quotes": make_quotes(codes, at),
                "previous_signal_codes": frozenset(codes[1:]),
                "previous_signal_as_of": at.replace(hour=8, minute=30),
            }
        )
    return values


def _case(tmp_path, settings, signal_kind: SignalKind):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    if signal_kind == SignalKind.MORNING_WATCHLIST:
        at = at.replace(hour=8, minute=30)
    store = RunInputSnapshotStore(tmp_path / "raw")
    captured = PointInTimeDataGateway(store, ReplayClock(at)).build_context(
        **_inputs(at, signal_kind)
    )
    registry = build_default_strategy_registry(settings)
    executor = StrategyExecutionRunner(registry)
    live_execution = executor.execute(captured.context)
    strategy = registry.resolve(signal_kind)
    run = SignalRun(
        **strategy.identity.attribution_fields(),
        run_id=captured.context.run_id,
        as_of=at,
        information_cutoff=at,
        signal_kind=signal_kind,
        status=RunStatus.DEGRADED,
        lifecycle_state=ResearchRunState.PUBLISHED,
        data_fresh=False,
        official_news_healthy=False,
        mainstream_news_healthy=False,
        tradable=False,
        candidate_count=len(
            [candidate for candidate in live_execution.result.candidates if not candidate.blocked]
        ),
    )
    manifest = ResearchRunManifest.create(
        run,
        captured.snapshots,
        live_execution.result_sha256,
        source_sha="a" * 40,
        source_dirty=False,
        project_version="0.2.0",
        python_version="3.13.0",
        dependency_lock_sha256="b" * 64,
        created_at=at,
    )
    return at, store, registry, executor, live_execution, manifest


@pytest.mark.parametrize("signal_kind", list(SignalKind))
def test_replay_runner_uses_shared_strategy_executor_and_matches_live_result_hash(
    tmp_path, settings, signal_kind
):
    at, store, _registry, executor, live_execution, manifest = _case(
        tmp_path, settings, signal_kind
    )
    snapshot_paths = [store.resolve(snapshot) for snapshot in manifest.input_snapshots]
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in snapshot_paths}

    replayed = ReplayRunner(
        ReplayDataGateway(store, ReplayClock(at)), executor
    ).run(manifest)

    assert replayed.result_sha256 == live_execution.result_sha256 == manifest.result_sha256
    assert replayed.result == live_execution.result
    assert replayed.identity == live_execution.identity
    assert before == {
        path: (path.stat().st_mtime_ns, path.stat().st_size) for path in snapshot_paths
    }


class _IdentityOverrideStrategy:
    def __init__(self, delegate, identity: StrategyIdentity):
        self.signal_kind = delegate.signal_kind
        self.requirements = delegate.requirements
        self.identity = identity
        self._delegate = delegate
        self.evaluate_calls = 0

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        self.evaluate_calls += 1
        return self._delegate.evaluate(context)


def test_replay_runner_rejects_strategy_identity_mismatch_before_evaluation(tmp_path, settings):
    at, store, registry, _executor, _live, manifest = _case(
        tmp_path, settings, SignalKind.MORNING_WATCHLIST
    )
    delegate = registry.resolve(SignalKind.MORNING_WATCHLIST)
    wrong = _IdentityOverrideStrategy(
        delegate,
        StrategyIdentity(
            "other-morning",
            delegate.identity.version,
            delegate.identity.parameter_snapshot,
        ),
    )
    runner = ReplayRunner(
        ReplayDataGateway(store, ReplayClock(at)),
        StrategyExecutionRunner(StrategyRegistry([wrong])),
    )

    with pytest.raises(ReplayExecutionViolation, match="Strategy Identity"):
        runner.run(manifest)

    assert wrong.evaluate_calls == 0


class _DriftingStrategy(_IdentityOverrideStrategy):
    def evaluate(self, context: StrategyContext) -> StrategyResult:
        original = super().evaluate(context)
        changed = [
            candidate.model_copy(
                update={"opportunity_score": max(0.0, candidate.opportunity_score - 1.0)}
            )
            for candidate in original.candidates
        ]
        return StrategyResult(
            changed,
            original.eligible_count,
            original.exclusion_counts,
            original.diagnostics,
        )


def test_replay_runner_fails_closed_when_shared_kernel_result_drifted(tmp_path, settings):
    at, store, registry, _executor, _live, manifest = _case(
        tmp_path, settings, SignalKind.PRECLOSE_ENTRY
    )
    delegate = registry.resolve(SignalKind.PRECLOSE_ENTRY)
    drifting = _DriftingStrategy(delegate, delegate.identity)
    runner = ReplayRunner(
        ReplayDataGateway(store, ReplayClock(at)),
        StrategyExecutionRunner(StrategyRegistry([drifting])),
    )

    with pytest.raises(ReplayExecutionViolation, match="result hash"):
        runner.run(manifest)

    assert drifting.evaluate_calls == 1


def test_shared_executor_rejects_candidates_from_another_run(tmp_path, settings):
    _at, _store, registry, _executor, _live, manifest = _case(
        tmp_path, settings, SignalKind.MORNING_WATCHLIST
    )
    delegate = registry.resolve(SignalKind.MORNING_WATCHLIST)

    class WrongRunStrategy(_IdentityOverrideStrategy):
        def evaluate(self, context: StrategyContext) -> StrategyResult:
            original = super().evaluate(context)
            return StrategyResult(
                [candidate.model_copy(update={"run_id": "other-run"}) for candidate in original.candidates],
                original.eligible_count,
                original.exclusion_counts,
                original.diagnostics,
            )

    wrong = WrongRunStrategy(delegate, delegate.identity)
    context = ReplayDataGateway(
        _store, ReplayClock(manifest.information_cutoff)
    ).load_context(manifest)

    with pytest.raises(ValueError, match="execution context run_id"):
        StrategyExecutionRunner(StrategyRegistry([wrong])).execute(context)


def test_replay_runner_rejects_manifest_parameter_hash_mismatch(tmp_path, settings):
    at, store, _registry, executor, _live, manifest = _case(
        tmp_path, settings, SignalKind.MORNING_WATCHLIST
    )
    inconsistent = manifest.model_copy(update={"parameter_hash": "c" * 64})

    with pytest.raises(ReplayExecutionViolation, match="parameter hash"):
        ReplayRunner(ReplayDataGateway(store, ReplayClock(at)), executor).run(inconsistent)


@pytest.mark.parametrize(
    "signal_kind",
    [SignalKind.MORNING_WATCHLIST, SignalKind.PRECLOSE_ENTRY],
)
def test_manifest_parameter_snapshot_must_match_parameter_hash(tmp_path, settings, signal_kind):
    at, store, _registry, executor, _live, manifest = _case(tmp_path, settings, signal_kind)
    changed_parameters = {**manifest.strategy_parameters, "future-only": True}
    changed_snapshot = StrategyParameterSnapshot.from_mapping(changed_parameters)
    changed = manifest.model_copy(
        update={
            "strategy_parameters": changed_parameters,
            "parameter_hash": changed_snapshot.parameter_hash,
        }
    )
    changed = ResearchRunManifest.create(
        SignalRun(
            **{
                "strategy_id": changed.strategy_id,
                "strategy_version": changed.strategy_version,
                "parameter_hash": changed.parameter_hash,
                "strategy_parameters": changed.strategy_parameters,
            },
            run_id=changed.run_id,
            as_of=changed.information_cutoff,
            information_cutoff=changed.information_cutoff,
            signal_kind=changed.signal_kind,
            status=RunStatus.DEGRADED,
            lifecycle_state=ResearchRunState.PUBLISHED,
            data_fresh=False,
            official_news_healthy=False,
            mainstream_news_healthy=False,
            tradable=False,
        ),
        changed.input_snapshots,
        changed.result_sha256,
        changed.source_sha,
        changed.source_dirty,
        changed.project_version,
        changed.python_version,
        changed.dependency_lock_sha256,
        changed.created_at,
    )

    with pytest.raises(ReplayExecutionViolation, match="Strategy Identity"):
        ReplayRunner(ReplayDataGateway(store, ReplayClock(at)), executor).run(changed)


@pytest.mark.parametrize(
    ("input_key", "future_value", "message"),
    [
        (
            "securities",
            lambda values, at: values["securities"].assign(
                list_date=at.date() + timedelta(days=1)
            ),
            "security",
        ),
        (
            "bars",
            lambda values, at: values["bars"].assign(trade_date=at.date()),
            "daily_bar",
        ),
        (
            "quotes",
            lambda values, at: values["quotes"].assign(
                captured_at=at + timedelta(microseconds=1)
            ),
            "live_quote",
        ),
        (
            "risk_events",
            lambda values, at: values["risk_events"].assign(
                published_at=at + timedelta(microseconds=1)
            ),
            "risk_event",
        ),
        (
            "previous_signal_as_of",
            lambda values, at: at + timedelta(microseconds=1),
            "previous_signal",
        ),
    ],
)
def test_future_input_perturbations_fail_before_snapshot_or_strategy_execution(
    tmp_path, input_key, future_value, message
):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    values = _inputs(at, SignalKind.PRECLOSE_ENTRY)
    values[input_key] = future_value(values, at)

    with pytest.raises(PointInTimeViolation, match=message):
        PointInTimeDataGateway(
            RunInputSnapshotStore(tmp_path / "raw"), ReplayClock(at)
        ).build_context(**values)

    assert not (tmp_path / "raw" / "run-inputs").exists()


@pytest.mark.parametrize("signal_kind", list(SignalKind))
def test_existing_manifest_result_is_unchanged_by_later_source_data(tmp_path, settings, signal_kind):
    at, store, _registry, executor, live_execution, manifest = _case(
        tmp_path, settings, signal_kind
    )
    later_source_data = _inputs(at, signal_kind)
    later_source_data["securities"] = later_source_data["securities"].assign(
        list_date=at.date() + timedelta(days=30)
    )
    later_source_data["bars"] = later_source_data["bars"].assign(
        trade_date=at.date() + timedelta(days=30)
    )
    later_source_data["risk_events"] = later_source_data["risk_events"].assign(
        published_at=at + timedelta(days=30)
    )
    if signal_kind == SignalKind.PRECLOSE_ENTRY:
        later_source_data["quotes"] = later_source_data["quotes"].assign(
            captured_at=at + timedelta(days=30)
        )
        later_source_data["previous_signal_codes"] = frozenset({"603999.SH"})

    replayed = ReplayRunner(
        ReplayDataGateway(store, ReplayClock(at)), executor
    ).run(manifest)

    assert replayed.result_sha256 == live_execution.result_sha256
    assert replayed.result == live_execution.result


@pytest.mark.parametrize("signal_kind", list(SignalKind))
def test_workflow_published_manifest_replays_through_the_same_kernel_without_side_effects(
    settings, signal_kind
):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    if signal_kind == SignalKind.MORNING_WATCHLIST:
        at = at.replace(hour=8, minute=30)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": at.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    quotes = make_quotes(codes, at) if signal_kind == SignalKind.PRECLOSE_ENTRY else pd.DataFrame()
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(quotes),
        llm_provider=FakeLLM(),
        clock=ReplayClock(at),
    )

    run = (
        workflow.run_preclose()
        if signal_kind == SignalKind.PRECLOSE_ENTRY
        else workflow.run_morning()
    )
    manifest = database.get_run_manifest(run.run_id)["manifest"]
    state_before = {
        table: len(database.table(table))
        for table in ("signal_runs", "candidate_scores", "paper_orders", "job_runs")
    }

    replayed = ReplayRunner(
        ReplayDataGateway(workflow.run_input_store, ReplayClock(at)),
        workflow.strategy_runner,
    ).run(manifest)

    assert replayed.result_sha256 == manifest.result_sha256
    assert replayed.identity.strategy_id == run.strategy_id
    assert replayed.identity.version == run.strategy_version
    assert state_before == {
        table: len(database.table(table))
        for table in ("signal_runs", "candidate_scores", "paper_orders", "job_runs")
    }
