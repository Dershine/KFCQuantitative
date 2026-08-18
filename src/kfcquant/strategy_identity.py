from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def validate_strategy_identifier(field_name: str, value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a 1-64 character strategy identifier")
    return value


def _normalize_parameter(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"strategy parameter {path} must be finite")
        return value
    if isinstance(value, Enum):
        return _normalize_parameter(value.value, path)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"strategy parameter key at {path} must be a string")
            normalized[key] = _normalize_parameter(item, f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_parameter(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"strategy parameter {path} has unsupported type {type(value).__name__}")


def canonical_parameter_json(parameters: Mapping[str, Any]) -> str:
    normalized = _normalize_parameter(parameters, "parameters")
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parameter_hash(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyParameterSnapshot:
    canonical_json: str
    parameter_hash: str

    def __post_init__(self) -> None:
        parsed = json.loads(self.canonical_json)
        if not isinstance(parsed, dict):
            raise ValueError("strategy parameter snapshot must be a JSON object")
        canonical = canonical_parameter_json(parsed)
        if canonical != self.canonical_json:
            raise ValueError("strategy parameter snapshot must use canonical JSON")
        if not _HASH.fullmatch(self.parameter_hash) or parameter_hash(canonical) != self.parameter_hash:
            raise ValueError("strategy parameter hash does not match the canonical snapshot")

    @classmethod
    def from_mapping(cls, parameters: Mapping[str, Any]) -> StrategyParameterSnapshot:
        canonical = canonical_parameter_json(parameters)
        return cls(canonical, parameter_hash(canonical))

    @classmethod
    def empty(cls) -> StrategyParameterSnapshot:
        return cls.from_mapping({})

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    strategy_id: str
    version: str
    parameter_snapshot: StrategyParameterSnapshot = StrategyParameterSnapshot(
        "{}", hashlib.sha256(b"{}").hexdigest()
    )

    def __post_init__(self) -> None:
        validate_strategy_identifier("strategy_id", self.strategy_id)
        validate_strategy_identifier("version", self.version)

    @property
    def parameter_hash(self) -> str:
        return self.parameter_snapshot.parameter_hash

    def attribution_fields(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.version,
            "parameter_hash": self.parameter_hash,
            "strategy_parameters": self.parameter_snapshot.as_dict(),
        }
