from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import typer

from kfcops import cli
from tests.test_m6b_assurance import m6b_settings


def test_recovery_and_capacity_cli_emit_reports_and_fail_on_incomplete_results(tmp_path, monkeypatch, capsys):
    settings = m6b_settings(tmp_path)
    monkeypatch.setattr(cli, "OpsSettings", lambda: settings)

    class Assurance:
        def __init__(self, *args):
            pass

        def run_recovery_drill(self, backup):
            return {"status": "passed", "report_path": "recovery.json"}

        def collect_capacity_baseline(self, query_samples=None):
            return {"status": "complete", "report_path": "baseline.json", "samples": query_samples}

    monkeypatch.setattr(cli, "AssuranceManager", Assurance)

    cli.recovery_drill(None, False)
    cli.capacity_baseline(3, True)

    output = capsys.readouterr().out
    assert "recovery.json" in output
    assert '"samples": 3' in output

    Assurance.run_recovery_drill = lambda self, backup: {"status": "failed", "report_path": "failed.json"}
    Assurance.collect_capacity_baseline = lambda self, query_samples=None: {
        "status": "partial",
        "report_path": "partial.json",
    }
    with pytest.raises(typer.Exit):
        cli.recovery_drill(None, False)
    with pytest.raises(typer.Exit):
        cli.capacity_baseline(None, False)


def test_capacity_decision_cli_restricts_input_and_forwards_declared_requirements(tmp_path, monkeypatch, capsys):
    settings = m6b_settings(tmp_path)
    baseline_dir = settings.assurance_directory / "capacity-baselines"
    baseline_dir.mkdir(parents=True)
    baseline = baseline_dir / "baseline.json"
    baseline.write_text(json.dumps({"record_sha256": "fixture"}), encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "OpsSettings", lambda: settings)

    class Assurance:
        def __init__(self, *args):
            pass

        def evaluate_capacity(self, payload, **kwargs):
            captured.update({"payload": payload, **kwargs})
            return {"report_path": "decision.json", "recommendation": "collect_more_evidence"}

    monkeypatch.setattr(cli, "AssuranceManager", Assurance)

    cli.capacity_decision(baseline, True, True, False)

    assert captured["multiple_writers_required"] is True
    assert captured["remote_transactions_required"] is True
    assert "decision.json" in capsys.readouterr().out
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(typer.BadParameter):
        cli.capacity_decision(outside, False, False, False)


def test_deploy_serve_and_bootstrap_provenance_cli_delegate_to_boundaries(tmp_path, monkeypatch, capsys):
    settings = m6b_settings(tmp_path)
    monkeypatch.setattr(cli, "OpsSettings", lambda: settings)
    calls: list[object] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(
        cli,
        "DeploymentManager",
        lambda *args: SimpleNamespace(
            deploy_now=lambda sha, approve_irreversible=False: calls.append((sha, approve_irreversible)) or 7
        ),
    )
    monkeypatch.setattr(
        cli,
        "write_release_manifest",
        lambda *args, **kwargs: {"manifest_sha256": "a" * 64},
    )
    release = tmp_path / "release"
    release.mkdir()

    cli.serve("127.0.0.1", 8600)
    cli.deploy("a" * 40, True)
    cli.write_release_provenance(release, "a" * 40, "2026-08-19T00:00:00+08:00")

    assert calls[1] == ("a" * 40, True)
    assert "deployment 7 succeeded" in capsys.readouterr().out
    with pytest.raises(typer.BadParameter):
        cli.write_release_provenance(tmp_path / "missing", "a" * 40, "time")
