from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, object]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def test_ci_quality_gate_dependencies_and_configuration_are_declared():
    configuration = _pyproject()
    project = configuration["project"]
    dev_dependencies = project["optional-dependencies"]["dev"]

    assert any(item.startswith("mypy") for item in dev_dependencies)
    assert any(item.startswith("bandit[") for item in dev_dependencies)
    assert any(item.startswith("pip-audit") for item in dev_dependencies)

    coverage = configuration["tool"]["coverage"]
    assert coverage["run"]["branch"] is True
    assert coverage["report"]["fail_under"] >= 84

    mypy = configuration["tool"]["mypy"]
    assert mypy["python_version"] == "3.12"
    assert mypy["check_untyped_defs"] is True
    assert mypy["disallow_untyped_defs"] is True
    assert "src/kfcquant/application/ports.py" in mypy["files"]
    assert "src/kfcquant/strategy/contracts.py" in mypy["files"]

    bandit = configuration["tool"]["bandit"]
    assert bandit["exclude_dirs"] == ["tests"]


def test_ci_workflow_blocks_coverage_type_security_and_dependency_regressions():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci-release.yml").read_text(encoding="utf-8")

    assert "pytest --cov=kfcquant --cov=kfcops" in workflow
    assert "python -m mypy" in workflow
    assert "python -m bandit -r src -c pyproject.toml -lll" in workflow
    assert "python scripts/scan_secrets.py --tracked" in workflow
    assert (
        "python -m pip_audit --strict --desc off --disable-pip --no-deps "
        "--requirement requirements.lock"
    ) in workflow
