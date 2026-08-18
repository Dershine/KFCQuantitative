from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Self
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kfcquant.models import CandidateScore, SignalKind, SignalRun

_SHA256_LENGTH = 64


class RunInputKind(StrEnum):
    SECURITY = "security"
    DAILY_BAR = "daily_bar"
    LIVE_QUOTE = "live_quote"
    RISK_EVENT = "risk_event"
    UNPROCESSED_OFFICIAL_CODE = "unprocessed_official_code"
    PREVIOUS_SIGNAL_CODE = "previous_signal_code"


class RunInputSnapshot(BaseModel):
    """An immutable, exact input frame consumed by one Strategy evaluation."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    dataset_kind: RunInputKind
    schema_version: str
    source: str
    captured_at: datetime
    information_cutoff: datetime
    snapshot_path: str
    content_sha256: str
    row_count: int = Field(ge=0)
    ingestion_batch_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("run input captured_at must be timezone-aware")
        if self.information_cutoff.tzinfo is None or self.information_cutoff.utcoffset() is None:
            raise ValueError("run input information_cutoff must be timezone-aware")
        snapshot_path = PurePosixPath(self.snapshot_path)
        if (
            snapshot_path.is_absolute()
            or ".." in snapshot_path.parts
            or "\\" in self.snapshot_path
        ):
            raise ValueError("run input snapshot_path must be a safe relative path")
        if len(self.content_sha256) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise ValueError("run input content_sha256 must be a lowercase SHA-256")
        if self.snapshot_id != self.content_sha256:
            raise ValueError("run input snapshot_id must equal its content SHA-256")
        if not self.schema_version.strip() or not self.source.strip():
            raise ValueError("run input schema_version and source must not be blank")
        if any(not batch_id.strip() for batch_id in self.ingestion_batch_ids):
            raise ValueError("run input ingestion batch IDs must not be blank")
        if len(set(self.ingestion_batch_ids)) != len(self.ingestion_batch_ids):
            raise ValueError("run input ingestion batch IDs must be unique")
        return self


class RunInputSnapshotStore:
    """Content-address exact Strategy inputs without rewriting identical frames."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def capture(
        self,
        dataset_kind: RunInputKind,
        schema_version: str,
        source: str,
        frame: pd.DataFrame,
        captured_at: datetime,
        information_cutoff: datetime,
        ingestion_batch_ids: tuple[str, ...] = (),
    ) -> RunInputSnapshot:
        target_dir = self.root / "run-inputs" / dataset_kind.value
        target_dir.mkdir(parents=True, exist_ok=True)
        temporary = target_dir / f".{uuid4().hex}.tmp.parquet"
        try:
            frame.to_parquet(temporary, index=False)
            content = temporary.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            target = target_dir / f"{digest}.parquet"
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise RuntimeError(f"content-addressed run input is corrupted: {target}")
            else:
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return RunInputSnapshot(
            snapshot_id=digest,
            dataset_kind=dataset_kind,
            schema_version=schema_version,
            source=source,
            captured_at=captured_at,
            information_cutoff=information_cutoff,
            snapshot_path=target.relative_to(self.root).as_posix(),
            content_sha256=digest,
            row_count=len(frame),
            ingestion_batch_ids=ingestion_batch_ids,
        )

    def resolve(self, snapshot: RunInputSnapshot) -> Path:
        root = self.root.resolve()
        path = (root / Path(snapshot.snapshot_path)).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("run input snapshot path escapes the configured raw-data root")
        return path

    def verify(self, snapshot: RunInputSnapshot) -> bool:
        path = self.resolve(snapshot)
        if not path.is_file():
            raise RuntimeError(f"run input snapshot is missing: {snapshot.snapshot_path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != snapshot.content_sha256:
            raise RuntimeError(
                f"run input snapshot hash mismatch for {snapshot.snapshot_id}: "
                f"expected {snapshot.content_sha256}, got {actual}"
            )
        return True


def candidate_result_sha256(candidates: list[CandidateScore]) -> str:
    payload = [
        candidate.model_dump(mode="json")
        for candidate in sorted(candidates, key=lambda item: (item.rank, item.ts_code))
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResearchRunManifest(BaseModel):
    """Immutable versions, inputs, and result identity for a Published Research Run."""

    model_config = ConfigDict(frozen=True)

    manifest_version: str = "research-run-manifest-v1"
    run_id: str
    signal_kind: SignalKind
    information_cutoff: datetime
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    strategy_parameters: dict[str, object]
    source_sha: str
    source_dirty: bool
    project_version: str
    python_version: str
    dependency_lock_sha256: str
    input_snapshots: tuple[RunInputSnapshot, ...]
    result_sha256: str
    created_at: datetime
    manifest_sha256: str

    @staticmethod
    def _canonical_payload(values: dict[str, object]) -> str:
        payload = {key: value for key, value in values.items() if key != "manifest_sha256"}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def create(
        cls,
        run: SignalRun,
        input_snapshots: tuple[RunInputSnapshot, ...],
        result_sha256: str,
        source_sha: str,
        source_dirty: bool,
        project_version: str,
        python_version: str,
        dependency_lock_sha256: str,
        created_at: datetime,
    ) -> ResearchRunManifest:
        values: dict[str, object] = {
            "manifest_version": "research-run-manifest-v1",
            "run_id": run.run_id,
            "signal_kind": run.signal_kind,
            "information_cutoff": run.information_cutoff or run.as_of,
            "strategy_id": run.strategy_id,
            "strategy_version": run.strategy_version,
            "parameter_hash": run.parameter_hash,
            "strategy_parameters": run.strategy_parameters,
            "source_sha": source_sha,
            "source_dirty": source_dirty,
            "project_version": project_version,
            "python_version": python_version,
            "dependency_lock_sha256": dependency_lock_sha256,
            "input_snapshots": input_snapshots,
            "result_sha256": result_sha256,
            "created_at": created_at,
        }
        serializable = cls.model_construct(**values, manifest_sha256="").model_dump(mode="json")
        values["manifest_sha256"] = hashlib.sha256(cls._canonical_payload(serializable).encode()).hexdigest()
        return cls(**values)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.information_cutoff.tzinfo is None or self.information_cutoff.utcoffset() is None:
            raise ValueError("run manifest information_cutoff must be timezone-aware")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("run manifest created_at must be timezone-aware")
        if not self.source_sha.strip() or len(self.source_sha) > 128:
            raise ValueError("run manifest source_sha must be a non-empty revision identity")
        if not self.project_version.strip() or not self.python_version.strip():
            raise ValueError("run manifest project and Python versions must not be blank")
        if not self.dependency_lock_sha256.strip():
            raise ValueError("run manifest dependency lock identity must not be blank")
        kinds = [snapshot.dataset_kind for snapshot in self.input_snapshots]
        if len(kinds) != len(set(kinds)):
            raise ValueError("run manifest must contain at most one exact snapshot per input kind")
        required = {
            RunInputKind.SECURITY,
            RunInputKind.DAILY_BAR,
            RunInputKind.RISK_EVENT,
            RunInputKind.UNPROCESSED_OFFICIAL_CODE,
        }
        if self.signal_kind == SignalKind.PRECLOSE_ENTRY:
            required.update({RunInputKind.LIVE_QUOTE, RunInputKind.PREVIOUS_SIGNAL_CODE})
        missing = sorted(kind.value for kind in required - set(kinds))
        if missing:
            raise ValueError(f"run manifest is missing required input snapshots: {missing}")
        for hash_value, label in (
            (self.parameter_hash, "parameter_hash"),
            (self.result_sha256, "result_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
        ):
            if len(hash_value) != _SHA256_LENGTH or any(
                character not in "0123456789abcdef" for character in hash_value
            ):
                raise ValueError(f"run manifest {label} must be a lowercase SHA-256")
        expected = hashlib.sha256(
            self._canonical_payload(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        if expected != self.manifest_sha256:
            raise ValueError("run manifest hash does not match its canonical payload")
        return self

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
