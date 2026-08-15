from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from kfcquant.db import Database
from kfcquant.services import workflow as workflow_module
from kfcquant.services.workflow import Workflow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("version_info", "supported"),
    [
        ((3, 12, 0), True),
        ((3, 13, 0), True),
        ((3, 11, 9), False),
    ],
)
def test_doctor_uses_declared_minimum_python_version(settings, monkeypatch, version_info, supported):
    monkeypatch.setattr(workflow_module, "PYTHON_VERSION_INFO", version_info)
    workflow = Workflow(
        settings,
        database=Database(settings.database_path, settings.initial_cash),
        market_provider=object(),
        live_provider=object(),
        news_provider=object(),
        llm_provider=object(),
    )

    python_check = next(item for item in workflow.doctor() if item["check"] == "python")

    assert python_check["ok"] is supported


def test_python_version_contract_is_consistent_across_entry_points():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.12"

    paths = [
        ".github/workflows/ci-release.yml",
        "README.md",
        "requirements.lock",
        "deploy/bootstrap_server.sh",
        "scripts/start_kfcquant.ps1",
    ]
    contracts = {path: (PROJECT_ROOT / path).read_text(encoding="utf-8") for path in paths}

    assert 'python-version: "3.12"' in contracts[".github/workflows/ci-release.yml"]
    assert "Python 3.12或更高版本" in contracts["README.md"]
    assert "Python 3.12+" in contracts["requirements.lock"]
    assert "sys.version_info < (3, 12)" in contracts["deploy/bootstrap_server.sh"]
    assert "sys.version_info >= (3, 12)" in contracts["scripts/start_kfcquant.ps1"]
    assert "3.13" not in contracts["scripts/start_kfcquant.ps1"]
