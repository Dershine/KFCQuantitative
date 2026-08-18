from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from kfcquant.market_data import MarketTableSchema, ValidatedMarketFrame

LOGGER = logging.getLogger(__name__)
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SnapshotIntegrityError(RuntimeError):
    """A captured snapshot is missing or no longer matches its manifest."""


class MarketDatasetKind(StrEnum):
    SECURITY = "security"
    TRADE_CALENDAR = "trade_calendar"
    DAILY_BAR = "daily_bar"
    LIVE_QUOTE = "live_quote"

    @classmethod
    def from_schema(cls, schema: MarketTableSchema) -> MarketDatasetKind:
        try:
            return cls(schema.name)
        except ValueError as exc:
            raise ValueError(f"unsupported ingestion schema: {schema.name}") from exc


@dataclass(frozen=True, slots=True)
class IngestionManifest:
    batch_id: str
    dataset_kind: MarketDatasetKind
    schema_version: str
    provider: str
    collected_at: datetime
    snapshot_path: Path
    content_sha256: str
    row_count: int
    quality_report_json: str
    job_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ValueError("ingestion batch_id must not be blank")
        if not _PROVIDER_ID.fullmatch(self.provider):
            raise ValueError(f"invalid provider identity: {self.provider!r}")
        if self.collected_at.tzinfo is None or self.collected_at.utcoffset() is None:
            raise ValueError("ingestion collected_at must be timezone-aware")
        if self.snapshot_path.is_absolute() or ".." in self.snapshot_path.parts:
            raise ValueError("ingestion snapshot_path must be a safe relative path")
        if not _SHA256.fullmatch(self.content_sha256):
            raise ValueError("ingestion content_sha256 must be a lowercase SHA-256")
        if self.row_count < 0:
            raise ValueError("ingestion row_count must be non-negative")
        report = json.loads(self.quality_report_json)
        if (
            not isinstance(report, dict)
            or report.get("validation_passed") is not True
            or report.get("schema") != self.dataset_kind.value
            or report.get("schema_version") != self.schema_version
            or report.get("row_count") != self.row_count
        ):
            raise ValueError("ingestion quality report must match the validated batch")

    @property
    def quality_report(self) -> dict[str, object]:
        return json.loads(self.quality_report_json)


def resolve_provider_identity(
    provider: object,
    validated: ValidatedMarketFrame | None = None,
) -> str:
    """Resolve the provider that actually produced a normalized batch."""
    if validated is not None and "source" in validated.frame.columns and not validated.frame.empty:
        sources = sorted({str(value).strip() for value in validated.frame["source"] if str(value).strip()})
        if len(sources) > 1:
            raise ValueError(f"normalized batch contains multiple provider sources: {sources}")
        identity = sources[0]
    else:
        identity = str(getattr(provider, "source_name", "")).strip()
    if not identity:
        raise ValueError("provider must expose non-empty source_name metadata")
    if not _PROVIDER_ID.fullmatch(identity):
        raise ValueError(f"invalid provider identity: {identity!r}")
    return identity


def _quality_report(validated: ValidatedMarketFrame) -> dict[str, object]:
    schema = validated.schema
    return {
        "validation_passed": True,
        "schema": schema.name,
        "schema_version": schema.version,
        "row_count": validated.row_count,
        "columns": list(schema.columns),
        "unique_key": list(schema.unique_key),
        "units": schema.units,
        "null_counts": {
            column: int(validated.frame[column].isna().sum()) for column in schema.columns
        },
    }


class IngestionSnapshotStore:
    """Write immutable normalized Parquet snapshots and verify their content hash."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def capture(
        self,
        validated: ValidatedMarketFrame,
        provider: str,
        collected_at: datetime,
        job_run_id: str | None = None,
    ) -> IngestionManifest:
        if not _PROVIDER_ID.fullmatch(provider):
            raise ValueError(f"invalid provider identity: {provider!r}")
        if collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("ingestion collected_at must be timezone-aware")
        batch_id = str(uuid4())
        kind = MarketDatasetKind.from_schema(validated.schema)
        relative = Path(provider) / kind.value / collected_at.strftime("%Y%m%d") / f"{batch_id}.parquet"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{batch_id}.{uuid4().hex}.tmp.parquet")
        try:
            validated.frame.to_parquet(temporary, index=False)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        report_json = json.dumps(
            _quality_report(validated),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest = IngestionManifest(
            batch_id=batch_id,
            dataset_kind=kind,
            schema_version=validated.schema.version,
            provider=provider,
            collected_at=collected_at,
            snapshot_path=relative,
            content_sha256=digest,
            row_count=validated.row_count,
            quality_report_json=report_json,
            job_run_id=job_run_id,
        )
        LOGGER.info(
            "captured ingestion batch batch_id=%s dataset=%s provider=%s rows=%s sha256=%s",
            batch_id,
            kind.value,
            provider,
            validated.row_count,
            digest,
        )
        return manifest

    def resolve(self, manifest: IngestionManifest) -> Path:
        root = self.root.resolve()
        path = (root / manifest.snapshot_path).resolve()
        if not path.is_relative_to(root):
            raise SnapshotIntegrityError("snapshot path escapes the configured raw-data root")
        return path

    def verify(self, manifest: IngestionManifest) -> bool:
        path = self.resolve(manifest)
        if not path.is_file():
            raise SnapshotIntegrityError(f"snapshot is missing: {manifest.snapshot_path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != manifest.content_sha256:
            raise SnapshotIntegrityError(
                f"snapshot hash mismatch for {manifest.batch_id}: expected {manifest.content_sha256}, got {actual}"
            )
        return True
