from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore
from kfcops.web import create_app


def ops_settings(tmp_path):
    return OpsSettings(
        database_path=tmp_path / "ops.sqlite3",
        compose_directory=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        release_env_file=tmp_path / ".release.env",
        research_database=tmp_path / "research.duckdb",
        backup_directory=tmp_path / "backups",
        github_repository="owner/repository",
        session_secret="test-secret",
    )


def test_manager_rejects_arbitrary_release_identifier(tmp_path):
    settings = ops_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    with pytest.raises(ValueError, match="40位"):
        manager.request_deploy("latest; rm -rf /tmp/example")


def test_ops_write_requires_csrf_and_confirmation(tmp_path, monkeypatch):
    app = create_app(ops_settings(tmp_path))
    manager = app.state.manager
    monkeypatch.setattr(manager, "releases", lambda: [])
    monkeypatch.setattr(
        manager,
        "runtime",
        lambda: {
            "active_sha": "",
            "previous_sha": "",
            "pending_sha": "",
            "compose_ps": "",
            "disk_free_bytes": 10_737_418_240,
            "certificate": {},
        },
    )
    monkeypatch.setattr(manager, "logs", lambda: "")
    called = []
    monkeypatch.setattr(manager, "restart", lambda: called.append(True))
    client = TestClient(app, base_url="https://testserver")

    assert client.post("/ops/actions/restart", data={"csrf": "bad", "confirm": "yes"}).status_code == 403
    page = client.get("/ops/")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/ops/actions/restart", data={"csrf": token, "confirm": "no"}).status_code == 400
    response = client.post("/ops/actions/restart", data={"csrf": token, "confirm": "yes"}, follow_redirects=False)
    assert response.status_code == 303
    assert called == [True]


def test_protected_window_creates_pending_request_without_execution(tmp_path, monkeypatch):
    settings = ops_settings(tmp_path)
    store = OpsStore(settings.database_path)
    manager = DeploymentManager(settings, store)
    sha = "a" * 40
    monkeypatch.setattr(manager, "_protected_window", lambda: True)

    status, _ = manager.request_deploy(sha)

    assert status == "pending"
    assert store.get("pending_sha") == sha
    assert store.recent_deployments(1)[0]["status"] == "pending"


def test_successful_deployment_records_rollback_material(tmp_path, monkeypatch):
    settings = ops_settings(tmp_path)
    settings.research_database.write_bytes(b"database snapshot")
    store = OpsStore(settings.database_path)
    store.set("active_sha", "b" * 40)
    manager = DeploymentManager(settings, store)
    target = "a" * 40
    deployment_id = store.create_deployment(target, "b" * 40, "checking", "test")
    monkeypatch.setattr(manager, "releases", lambda: [{"sha": target, "deployable": True}])
    monkeypatch.setattr(manager, "_research_job_running", lambda: False)
    monkeypatch.setattr(manager, "_run_compose", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(manager, "_wait_healthy", lambda: None)

    manager._deploy(deployment_id, target, "b" * 40)

    assert store.get("active_sha") == target
    assert store.get("previous_sha") == "b" * 40
    assert store.get("previous_backup")
    assert store.recent_deployments(1)[0]["status"] == "succeeded"
