from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SecretFinding:
    path: Path
    line_number: int
    rule: str
    matched_text: str = "[REDACTED]"


_HIGH_CONFIDENCE_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b")),
    ("openai-compatible-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
)
_ASSIGNMENT = re.compile(
    r"(?i)(?:[a-z0-9_-]*(?:api[_-]?key|token|password|secret))\b"
    r"\s*[:=]\s*['\"]?([a-z0-9_+/=-]{20,})"
)
_PLACEHOLDERS = (
    "change-me",
    "example",
    "fixture",
    "placeholder",
    "redacted",
    "settings.",
    "environ",
    "getenv",
    "test-",
    "top-secret",
    "your-",
    "你的",
)


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDERS)


def scan_paths(paths: list[Path]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in sorted({item.resolve() for item in paths}):
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if "pragma: allowlist secret" in line.lower():
                continue
            high_confidence_match = False
            for rule, pattern in _HIGH_CONFIDENCE_PATTERNS:
                if pattern.search(line):
                    findings.append(SecretFinding(path, line_number, rule))
                    high_confidence_match = True
            if high_confidence_match:
                continue
            assignment = _ASSIGNMENT.search(line)
            if assignment and not _is_placeholder(assignment.group(1)):
                findings.append(SecretFinding(path, line_number, "credential-assignment"))
    return findings


def tracked_paths(repository: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    return [repository / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def release_python(release: Path) -> Path:
    return release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def release_application(release: Path) -> Path:
    return release / ".venv" / ("Scripts/kfcquant.exe" if os.name == "nt" else "bin/kfcquant")


def write_release_manifest(
    release: Path,
    sha: str,
    *,
    source_commit_time: str,
    workflow: dict[str, object],
    run_command: Callable[[list[str]], str] | None = None,
    built_at: datetime | None = None,
) -> dict[str, Any]:
    runner = run_command or _default_run_command
    lock_file = release / "requirements.lock"
    if not lock_file.is_file():
        raise RuntimeError("Release provenance requires requirements.lock")
    python = release_python(release)
    application = release_application(release)
    python_version = runner([str(python), "--version"]).strip()
    installed = sorted(
        line.strip()
        for line in runner([str(python), "-m", "pip", "freeze", "--all"]).splitlines()
        if line.strip()
    )
    migration_output = runner([str(application), "migration-contract", "--json"])
    try:
        migration_contract = json.loads(migration_output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Release migration contract is not valid JSON") from exc
    if not isinstance(migration_contract, dict):
        raise RuntimeError("Release migration contract must be a JSON object")
    conclusion = workflow.get("conclusion")
    if conclusion not in {"success", "bootstrap"}:
        raise RuntimeError("Release provenance requires successful workflow or bootstrap evidence")
    if conclusion == "success" and (
        not isinstance(workflow.get("id"), int)
        or not str(workflow.get("url", "")).startswith("https://")
    ):
        raise RuntimeError("Release provenance requires a verifiable successful workflow run")
    payload: dict[str, Any] = {
        "manifest_version": 1,
        "source_sha": sha,
        "source_commit_time": source_commit_time,
        "built_at": (built_at or datetime.now(UTC)).isoformat(),
        "python_version": python_version,
        "requirements_lock_sha256": _file_sha256(lock_file),
        "installed_packages": installed,
        "installed_packages_sha256": _canonical_sha256(installed),
        "migration_contract": migration_contract,
        "migration_contract_sha256": _canonical_sha256(migration_contract),
        "workflow": workflow,
    }
    payload["manifest_sha256"] = _canonical_sha256(payload)
    target = release / ".release-manifest.json"
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload


def verify_release_manifest(release: Path, sha: str) -> bool:
    target = release / ".release-manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        manifest_hash = payload.pop("manifest_sha256")
        workflow = payload.get("workflow")
        return bool(
            payload.get("manifest_version") == 1
            and payload.get("source_sha") == sha
            and manifest_hash == _canonical_sha256(payload)
            and payload.get("requirements_lock_sha256") == _file_sha256(release / "requirements.lock")
            and payload.get("installed_packages_sha256")
            == _canonical_sha256(payload.get("installed_packages"))
            and payload.get("migration_contract_sha256")
            == _canonical_sha256(payload.get("migration_contract"))
            and isinstance(workflow, dict)
            and workflow.get("conclusion") in {"success", "bootstrap"}
            and (
                workflow.get("conclusion") == "bootstrap"
                or (
                    isinstance(workflow.get("id"), int)
                    and str(workflow.get("url", "")).startswith("https://")
                )
            )
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _default_run_command(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    return f"{result.stdout}\n{result.stderr}".strip()
