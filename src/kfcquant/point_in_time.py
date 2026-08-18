from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from kfcquant.ingestion import IngestionManifest, MarketDatasetKind
from kfcquant.market_data import DAILY_BAR_SCHEMA, LIVE_QUOTE_SCHEMA, SECURITY_SCHEMA
from kfcquant.models import SignalKind
from kfcquant.run_manifest import RunInputKind, RunInputSnapshot, RunInputSnapshotStore
from kfcquant.strategy import StrategyContext


class PointInTimeViolation(ValueError):
    """An input was not proven to be available at the Research Run cutoff."""


@dataclass(frozen=True, slots=True)
class PointInTimeContext:
    context: StrategyContext
    snapshots: tuple[RunInputSnapshot, ...]


class PointInTimeDataGateway:
    """Validate availability time and capture the exact inputs supplied to Strategy."""

    def __init__(self, snapshot_store: RunInputSnapshotStore):
        self.snapshot_store = snapshot_store

    @staticmethod
    def _require_aware(label: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PointInTimeViolation(f"{label} must be timezone-aware")

    @staticmethod
    def _validate_date_column(
        dataset: str,
        frame: pd.DataFrame,
        column: str,
        cutoff: datetime,
        *,
        same_day_is_future: bool,
    ) -> None:
        if frame.empty:
            return
        values = pd.to_datetime(frame[column], errors="coerce")
        if values.isna().any():
            raise PointInTimeViolation(f"{dataset} contains an invalid {column}")
        dates = values.dt.date
        invalid = dates >= cutoff.date() if same_day_is_future else dates > cutoff.date()
        if invalid.any():
            raise PointInTimeViolation(f"{dataset} contains data after information_cutoff")

    @staticmethod
    def _validate_timestamp_column(dataset: str, frame: pd.DataFrame, column: str, cutoff: datetime) -> None:
        if frame.empty:
            return
        values = pd.to_datetime(frame[column], errors="coerce", utc=True)
        if values.isna().any():
            raise PointInTimeViolation(f"{dataset} contains an invalid {column}")
        if (values > pd.Timestamp(cutoff).tz_convert("UTC")).any():
            raise PointInTimeViolation(f"{dataset} contains data after information_cutoff")

    def build_context(
        self,
        *,
        run_id: str,
        signal_kind: SignalKind,
        as_of: datetime,
        information_cutoff: datetime,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        quotes: pd.DataFrame | None = None,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: frozenset[str] = frozenset(),
        previous_signal_codes: frozenset[str] = frozenset(),
        previous_signal_as_of: datetime | None = None,
        quote_ingestion_manifest: IngestionManifest | None = None,
        captured_at: datetime | None = None,
    ) -> PointInTimeContext:
        self._require_aware("as_of", as_of)
        self._require_aware("information_cutoff", information_cutoff)
        if information_cutoff > as_of:
            raise PointInTimeViolation("information_cutoff cannot be after as_of")
        quote_frame = quotes.copy() if quotes is not None else pd.DataFrame()
        risk_frame = (
            risk_events.copy()
            if risk_events is not None
            else pd.DataFrame(columns=["event_id", "ts_code", "published_at"])
        )
        self._validate_date_column(
            "security", securities, "list_date", information_cutoff, same_day_is_future=False
        )
        self._validate_date_column(
            "daily_bar", bars, "trade_date", information_cutoff, same_day_is_future=True
        )
        self._validate_timestamp_column("live_quote", quote_frame, "captured_at", information_cutoff)
        self._validate_timestamp_column("risk_event", risk_frame, "published_at", information_cutoff)
        if previous_signal_as_of is not None:
            self._require_aware("previous_signal_as_of", previous_signal_as_of)
            if previous_signal_as_of > information_cutoff:
                raise PointInTimeViolation("previous_signal is after information_cutoff")
        if quote_ingestion_manifest is not None:
            if quote_ingestion_manifest.dataset_kind != MarketDatasetKind.LIVE_QUOTE:
                raise PointInTimeViolation("live_quote references a non-quote ingestion batch")
            if quote_ingestion_manifest.schema_version != LIVE_QUOTE_SCHEMA.version:
                raise PointInTimeViolation("live_quote ingestion schema does not match the Strategy input")
            if quote_ingestion_manifest.row_count != len(quote_frame):
                raise PointInTimeViolation("live_quote ingestion row count does not match the Strategy input")

        captured_at = captured_at or datetime.now(information_cutoff.tzinfo)
        common = {
            "captured_at": captured_at,
            "information_cutoff": information_cutoff,
        }
        snapshots = [
            self.snapshot_store.capture(
                RunInputKind.SECURITY,
                SECURITY_SCHEMA.version,
                "duckdb-normalized-store",
                securities,
                **common,
            ),
            self.snapshot_store.capture(
                RunInputKind.DAILY_BAR,
                DAILY_BAR_SCHEMA.version,
                "duckdb-normalized-store",
                bars,
                **common,
            ),
            self.snapshot_store.capture(
                RunInputKind.RISK_EVENT,
                "risk-event-v2",
                "duckdb-normalized-store",
                risk_frame,
                **common,
            ),
            self.snapshot_store.capture(
                RunInputKind.UNPROCESSED_OFFICIAL_CODE,
                "unprocessed-official-code-v1",
                "duckdb-normalized-store",
                pd.DataFrame({"ts_code": sorted(unprocessed_official_codes)}),
                **common,
            ),
        ]
        if signal_kind == SignalKind.PRECLOSE_ENTRY:
            quote_sources = sorted(set(quote_frame.get("source", pd.Series(dtype=str)).astype(str)))
            quote_source = (
                quote_sources[0]
                if len(quote_sources) == 1
                else quote_ingestion_manifest.provider
                if quote_ingestion_manifest is not None
                else "duckdb-normalized-store"
            )
            if (
                quote_ingestion_manifest is not None
                and quote_ingestion_manifest.provider != quote_source
            ):
                raise PointInTimeViolation("live_quote ingestion provider does not match the Strategy input")
            batch_ids = (quote_ingestion_manifest.batch_id,) if quote_ingestion_manifest is not None else ()
            snapshots.extend(
                [
                    self.snapshot_store.capture(
                        RunInputKind.LIVE_QUOTE,
                        LIVE_QUOTE_SCHEMA.version,
                        quote_source,
                        quote_frame,
                        ingestion_batch_ids=batch_ids,
                        **common,
                    ),
                    self.snapshot_store.capture(
                        RunInputKind.PREVIOUS_SIGNAL_CODE,
                        "previous-signal-code-v1",
                        "published-signal-run",
                        pd.DataFrame({"ts_code": sorted(previous_signal_codes)}),
                        **common,
                    ),
                ]
            )
        context = StrategyContext(
            run_id=run_id,
            signal_kind=signal_kind,
            as_of=as_of,
            information_cutoff=information_cutoff,
            securities=securities,
            bars=bars,
            quotes=quote_frame,
            risk_events=risk_frame,
            unprocessed_official_codes=unprocessed_official_codes,
            previous_signal_codes=previous_signal_codes,
        )
        return PointInTimeContext(context=context, snapshots=tuple(snapshots))
