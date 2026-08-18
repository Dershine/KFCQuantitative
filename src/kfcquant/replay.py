from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pandas as pd

from kfcquant.clock import Clock
from kfcquant.market_data import DAILY_BAR_SCHEMA, LIVE_QUOTE_SCHEMA, SECURITY_SCHEMA
from kfcquant.models import SignalKind
from kfcquant.point_in_time import PointInTimeDataGateway, PointInTimeViolation
from kfcquant.run_manifest import ResearchRunManifest, RunInputKind, RunInputSnapshot, RunInputSnapshotStore
from kfcquant.strategy import (
    StrategyContext,
    StrategyExecution,
    StrategyExecutionRunner,
    StrategyExecutionViolation,
    StrategyIdentity,
    StrategyParameterSnapshot,
)


class ReplayInputViolation(ValueError):
    """A manifest input cannot be proven safe and exact enough for replay."""


class ReplayExecutionViolation(ValueError):
    """A Replay cannot reproduce the exact Strategy identity and result."""


_SCHEMA_VERSIONS: Mapping[RunInputKind, str] = {
    RunInputKind.SECURITY: SECURITY_SCHEMA.version,
    RunInputKind.DAILY_BAR: DAILY_BAR_SCHEMA.version,
    RunInputKind.LIVE_QUOTE: LIVE_QUOTE_SCHEMA.version,
    RunInputKind.RISK_EVENT: "risk-event-v2",
    RunInputKind.UNPROCESSED_OFFICIAL_CODE: "unprocessed-official-code-v1",
    RunInputKind.PREVIOUS_SIGNAL_CODE: "previous-signal-code-v1",
}
_COMMON_INPUTS = {
    RunInputKind.SECURITY,
    RunInputKind.DAILY_BAR,
    RunInputKind.RISK_EVENT,
    RunInputKind.UNPROCESSED_OFFICIAL_CODE,
}
_PRECLOSE_INPUTS = _COMMON_INPUTS | {
    RunInputKind.LIVE_QUOTE,
    RunInputKind.PREVIOUS_SIGNAL_CODE,
}


class ReplayDataGateway:
    """Rebuild StrategyContext exclusively from immutable Run Manifest inputs."""

    def __init__(self, snapshot_store: RunInputSnapshotStore, clock: Clock):
        self.snapshot_store = snapshot_store
        self.clock = clock

    @staticmethod
    def _code_set(frame: pd.DataFrame, dataset_kind: RunInputKind) -> frozenset[str]:
        if list(frame.columns) != ["ts_code"]:
            raise ReplayInputViolation(f"{dataset_kind.value} must contain only the ts_code column")
        if frame["ts_code"].isna().any():
            raise ReplayInputViolation(f"{dataset_kind.value} contains a null ts_code")
        values = frame["ts_code"].astype(str).str.strip()
        if (values == "").any() or values.duplicated().any():
            raise ReplayInputViolation(f"{dataset_kind.value} contains blank or duplicate codes")
        return frozenset(values)

    def _read(self, snapshot: RunInputSnapshot, manifest: ResearchRunManifest) -> pd.DataFrame:
        expected_schema = _SCHEMA_VERSIONS[snapshot.dataset_kind]
        if snapshot.schema_version != expected_schema:
            raise ReplayInputViolation(
                f"{snapshot.dataset_kind.value} schema version mismatch: "
                f"expected {expected_schema}, got {snapshot.schema_version}"
            )
        if snapshot.information_cutoff != manifest.information_cutoff:
            raise ReplayInputViolation(
                f"{snapshot.dataset_kind.value} information cutoff does not match the Run Manifest"
            )
        try:
            return self.snapshot_store.read(snapshot)
        except Exception as exc:
            raise ReplayInputViolation(str(exc)) from exc

    @staticmethod
    def _restore_logical_dates(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
        """Restore Parquet/DuckDB timestamp round-trips to the declared date-only type."""
        restored = frame.copy()
        for column in columns:
            if column not in restored.columns:
                continue
            values = []
            for value in restored[column].tolist():
                if pd.isna(value):
                    values.append(None)
                elif isinstance(value, (datetime, pd.Timestamp)):
                    values.append(value.date())
                else:
                    values.append(value)
            restored[column] = pd.Series(values, index=restored.index, dtype=object)
        return restored

    def load_context(self, manifest: ResearchRunManifest) -> StrategyContext:
        replay_at = self.clock.now()
        if replay_at.tzinfo is None or replay_at.utcoffset() is None:
            raise ReplayInputViolation("ReplayClock must return a timezone-aware time")
        if replay_at != manifest.information_cutoff:
            raise ReplayInputViolation("ReplayClock must equal the Run Manifest information cutoff")

        expected_kinds = (
            _PRECLOSE_INPUTS if manifest.signal_kind == SignalKind.PRECLOSE_ENTRY else _COMMON_INPUTS
        )
        snapshots = {snapshot.dataset_kind: snapshot for snapshot in manifest.input_snapshots}
        if set(snapshots) != expected_kinds:
            missing = sorted(kind.value for kind in expected_kinds - set(snapshots))
            unexpected = sorted(kind.value for kind in set(snapshots) - expected_kinds)
            raise ReplayInputViolation(
                f"Run Manifest input kinds do not match {manifest.signal_kind.value}: "
                f"missing={missing}, unexpected={unexpected}"
            )

        frames = {kind: self._read(snapshot, manifest) for kind, snapshot in snapshots.items()}
        try:
            securities = SECURITY_SCHEMA.validate(
                self._restore_logical_dates(
                    frames[RunInputKind.SECURITY],
                    ("list_date", "delist_date"),
                )
            ).frame
            bars = DAILY_BAR_SCHEMA.validate(
                self._restore_logical_dates(
                    frames[RunInputKind.DAILY_BAR],
                    ("trade_date",),
                )
            ).frame
            quotes = (
                LIVE_QUOTE_SCHEMA.validate(frames[RunInputKind.LIVE_QUOTE]).frame
                if manifest.signal_kind == SignalKind.PRECLOSE_ENTRY
                else pd.DataFrame()
            )
        except Exception as exc:
            raise ReplayInputViolation(f"replay market input schema validation failed: {exc}") from exc

        risk_events = frames[RunInputKind.RISK_EVENT]
        required_risk_columns = {"event_id", "ts_code", "published_at"}
        if not required_risk_columns <= set(risk_events.columns):
            missing = sorted(required_risk_columns - set(risk_events.columns))
            raise ReplayInputViolation(f"risk_event is missing required columns: {missing}")
        unprocessed = self._code_set(
            frames[RunInputKind.UNPROCESSED_OFFICIAL_CODE],
            RunInputKind.UNPROCESSED_OFFICIAL_CODE,
        )
        previous = (
            self._code_set(
                frames[RunInputKind.PREVIOUS_SIGNAL_CODE],
                RunInputKind.PREVIOUS_SIGNAL_CODE,
            )
            if manifest.signal_kind == SignalKind.PRECLOSE_ENTRY
            else frozenset()
        )
        try:
            PointInTimeDataGateway.validate_strategy_inputs(
                as_of=replay_at,
                information_cutoff=manifest.information_cutoff,
                securities=securities,
                bars=bars,
                quotes=quotes,
                risk_events=risk_events,
            )
        except PointInTimeViolation as exc:
            raise ReplayInputViolation(str(exc)) from exc
        return StrategyContext(
            run_id=manifest.run_id,
            signal_kind=manifest.signal_kind,
            as_of=replay_at,
            information_cutoff=manifest.information_cutoff,
            securities=securities,
            bars=bars,
            quotes=quotes,
            risk_events=risk_events,
            unprocessed_official_codes=unprocessed,
            previous_signal_codes=previous,
        )


class ReplayRunner:
    """Execute a Run Manifest through the same deterministic kernel as live Workflow."""

    def __init__(
        self,
        data_gateway: ReplayDataGateway,
        strategy_runner: StrategyExecutionRunner,
    ) -> None:
        self.data_gateway = data_gateway
        self.strategy_runner = strategy_runner

    @staticmethod
    def _manifest_identity(manifest: ResearchRunManifest) -> StrategyIdentity:
        try:
            parameter_snapshot = StrategyParameterSnapshot.from_mapping(
                manifest.strategy_parameters
            )
            if parameter_snapshot.parameter_hash != manifest.parameter_hash:
                raise ValueError("parameter hash does not match the parameter snapshot")
            return StrategyIdentity(
                manifest.strategy_id,
                manifest.strategy_version,
                parameter_snapshot,
            )
        except (TypeError, ValueError) as exc:
            raise ReplayExecutionViolation(
                f"Run Manifest contains an invalid Strategy Identity: {exc}"
            ) from exc

    def run(self, manifest: ResearchRunManifest) -> StrategyExecution:
        expected_identity = self._manifest_identity(manifest)
        context = self.data_gateway.load_context(manifest)
        try:
            execution = self.strategy_runner.execute(
                context,
                expected_identity=expected_identity,
            )
        except StrategyExecutionViolation as exc:
            raise ReplayExecutionViolation(str(exc)) from exc
        if execution.result_sha256 != manifest.result_sha256:
            raise ReplayExecutionViolation(
                "Replay result hash does not match the immutable Run Manifest"
            )
        return execution
