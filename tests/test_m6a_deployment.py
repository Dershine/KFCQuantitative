from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore
from kfcquant.db import MIGRATIONS, Database
from kfcquant.migrations import RollbackPolicy, migration_contract


def m6_settings(tmp_path: Path) -> OpsSettings:
    return OpsSettings(
        database_path=tmp_path / "ops.sqlite3",
        deployment_lock=tmp_path / "deploy.lock",
        repository_directory=tmp_path / "repository",
        releases_directory=tmp_path / "releases",
        current_release_link=tmp_path / "current",
        builder_python=Path(os.sys.executable),
        research_database=tmp_path / "research.duckdb",
        research_lock=tmp_path / "database.lock",
        backup_directory=tmp_path / "backups",
        github_repository="owner/repository",
        session_secret="test-secret-that-is-at-least-32-bytes",
    )


def contract(*, latest: int, policy: str = "backup_restore") -> dict[str, object]:
    return {
        "contract_version": 1,
        "latest_schema_version": latest,
        "migrations": [
            {
                "version": version,
                "name": f"migration_{version}",
                "statements_sha256": f"{version:064x}",
                "rollback_policy": policy if version == latest else "backup_restore",
                "rollback_reason": "test policy",
            }
            for version in range(1, latest + 1)
        ],
    }


def test_migration_registry_has_explicit_machine_readable_rollback_policy():
    payload = migration_contract(MIGRATIONS)

    assert payload["latest_schema_version"] == len(MIGRATIONS)
    assert len(payload["migrations"]) == len(MIGRATIONS)
    assert {item["rollback_policy"] for item in payload["migrations"]} <= {
        RollbackPolicy.IN_PLACE.value,
        RollbackPolicy.BACKUP_RESTORE.value,
        RollbackPolicy.REQUIRES_APPROVAL.value,
    }
    assert all(item["rollback_reason"] for item in payload["migrations"])


def test_ops_settings_reject_overlapping_release_paths(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        OpsSettings(
            database_path=tmp_path / "ops.sqlite3",
            repository_directory=tmp_path / "same",
            releases_directory=tmp_path / "same",
            current_release_link=tmp_path / "current",
            github_repository="owner/repository",
            session_secret="test-secret-that-is-at-least-32-bytes",
        )


def test_github_release_listing_marks_only_successful_main_workflow(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    settings.github_token = "secret-token"
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    deployable = "a" * 40
    blocked = "b" * 40
    captured_headers: dict[str, str] = {}

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *, timeout, headers):
            assert timeout == 15
            captured_headers.update(headers)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params):
            if url.endswith("/commits"):
                assert params["sha"] == "main"
                return Response(
                    [
                        {
                            "sha": deployable,
                            "commit": {"message": "ready\ndetail", "committer": {"date": "2026-08-19"}},
                        },
                        {
                            "sha": blocked,
                            "commit": {"message": "blocked", "committer": {"date": "2026-08-18"}},
                        },
                    ]
                )
            assert params["branch"] == "main"
            return Response(
                {
                    "workflow_runs": [
                        {"head_sha": deployable, "name": "test-and-release", "conclusion": "success"},
                        {"head_sha": blocked, "name": "test-and-release", "conclusion": "failure"},
                    ]
                }
            )

    monkeypatch.setattr("kfcops.deployment.httpx.Client", Client)

    releases = manager.releases()

    assert captured_headers["Authorization"] == "Bearer secret-token"
    assert releases[0] == {
        "sha": deployable,
        "short_sha": deployable[:12],
        "message": "ready",
        "date": "2026-08-19",
        "deployable": True,
    }
    assert releases[1]["deployable"] is False


def test_compatibility_matrix_requires_backup_restore_for_schema_upgrade(tmp_path):
    manager = DeploymentManager(m6_settings(tmp_path), OpsStore(tmp_path / "ops.sqlite3"))

    matrix = manager._assess_migration_compatibility(contract(latest=10), contract(latest=11), 10, False)

    assert matrix["allowed"] is True
    assert matrix["rollback_strategy"] == "restore_deployment_backup"
    assert matrix["pending_versions"] == [11]
    assert matrix["approval_required"] is False


def test_compatibility_matrix_blocks_downgrade_and_unapproved_irreversible_change(tmp_path):
    manager = DeploymentManager(m6_settings(tmp_path), OpsStore(tmp_path / "ops.sqlite3"))

    downgrade = manager._assess_migration_compatibility(contract(latest=11), contract(latest=10), 11, False)
    irreversible = manager._assess_migration_compatibility(
        contract(latest=10), contract(latest=11, policy="requires_approval"), 10, False
    )
    approved = manager._assess_migration_compatibility(
        contract(latest=10), contract(latest=11, policy="requires_approval"), 10, True
    )

    assert downgrade["allowed"] is False
    assert "downgrade" in str(downgrade["reason"]).lower()
    assert irreversible["allowed"] is False
    assert irreversible["approval_required"] is True
    assert approved["allowed"] is True
    assert approved["approval_recorded"] is True


def test_compatibility_matrix_blocks_rewriting_an_applied_migration(tmp_path):
    manager = DeploymentManager(m6_settings(tmp_path), OpsStore(tmp_path / "ops.sqlite3"))
    active = contract(latest=10)
    target = contract(latest=10)
    target["migrations"][4]["statements_sha256"] = "f" * 64

    matrix = manager._assess_migration_compatibility(active, target, 10, False)

    assert matrix["allowed"] is False
    assert "drift" in str(matrix["reason"])


def test_preflight_migrates_only_a_disposable_database_copy(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    Database(settings.research_database).initialize()
    original_bytes = settings.research_database.read_bytes()
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    active = tmp_path / "releases" / ("b" * 40)
    target = tmp_path / "releases" / ("a" * 40)
    active.mkdir(parents=True)
    target.mkdir(parents=True)
    observed: dict[str, object] = {}

    monkeypatch.setattr(manager, "_release_contract", lambda release: contract(latest=10))

    def fake_run(release, *arguments, **kwargs):
        assert release == target
        assert arguments[0] == "migrate"
        copy_path = Path(arguments[2])
        observed["copy"] = copy_path
        assert copy_path != settings.research_database
        with duckdb.connect(str(copy_path)) as connection:
            connection.execute("CREATE TABLE preflight_probe(value INTEGER)")
        return "schema version: 10"

    monkeypatch.setattr(manager, "_run_release_application", fake_run)

    matrix = manager._preflight_migrations(active, target, approve_irreversible=False)

    assert matrix["allowed"] is True
    assert settings.research_database.read_bytes() == original_bytes
    assert not Path(observed["copy"]).exists()


def test_preflight_executes_target_cli_against_copy_end_to_end(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    Database(settings.research_database).initialize()
    original_bytes = settings.research_database.read_bytes()
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    active = settings.releases_directory / ("b" * 40)
    target = settings.releases_directory / ("a" * 40)
    active.mkdir(parents=True)
    target.mkdir(parents=True)
    executable = Path(os.sys.executable).parent / ("kfcquant.exe" if os.name == "nt" else "kfcquant")
    monkeypatch.setattr(manager, "_application_executable", lambda release: executable)

    matrix = manager._preflight_migrations(active, target, approve_irreversible=False)

    assert matrix["copy_migration_verified"] is True
    assert matrix["rollback_strategy"] == "no_schema_change"
    assert settings.research_database.read_bytes() == original_bytes


def test_build_or_preflight_failure_never_stops_services_or_switches_current(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    store = OpsStore(settings.database_path)
    previous_sha = "b" * 40
    target_sha = "a" * 40
    previous = settings.releases_directory / previous_sha
    previous.mkdir(parents=True)
    store.set("active_sha", previous_sha)
    manager = DeploymentManager(settings, store)
    deployment_id = store.create_deployment(target_sha, previous_sha, "checking", "test")
    service_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(manager, "_active_release_path", lambda: previous)
    monkeypatch.setattr(manager, "releases", lambda: [{"sha": target_sha, "deployable": True}])
    monkeypatch.setattr(manager, "_research_job_running", lambda: False)
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(manager, "_build_release", lambda sha: settings.releases_directory / sha)
    monkeypatch.setattr(
        manager,
        "_preflight_migrations",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("preflight rejected")),
    )
    monkeypatch.setattr(manager, "_run_service", lambda *args, **kwargs: service_calls.append(args) or "ok")

    manager._deploy(deployment_id, target_sha, previous_sha, False)

    assert service_calls == []
    assert manager._active_release_path() == previous
    assert store.deployment(deployment_id)["status"] == "failed"


def test_release_build_failure_leaves_active_service_running(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    store = OpsStore(settings.database_path)
    previous_sha = "b" * 40
    target_sha = "a" * 40
    previous = settings.releases_directory / previous_sha
    previous.mkdir(parents=True)
    store.set("active_sha", previous_sha)
    manager = DeploymentManager(settings, store)
    deployment_id = store.create_deployment(target_sha, previous_sha, "checking", "test")
    service_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(manager, "_active_release_path", lambda: previous)
    monkeypatch.setattr(manager, "releases", lambda: [{"sha": target_sha, "deployable": True}])
    monkeypatch.setattr(manager, "_research_job_running", lambda: False)
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        manager,
        "_build_release",
        lambda sha: (_ for _ in ()).throw(RuntimeError("dependency installation failed")),
    )
    monkeypatch.setattr(manager, "_run_service", lambda *args, **kwargs: service_calls.append(args) or "ok")

    manager._deploy(deployment_id, target_sha, previous_sha, False)

    assert service_calls == []
    assert manager._active_release_path() == previous
    assert store.deployment(deployment_id)["status"] == "failed"


def test_successful_deployment_switches_only_after_migration_and_rolls_back_link_on_health_failure(
    tmp_path, monkeypatch
):
    settings = m6_settings(tmp_path)
    settings.research_database.write_bytes(b"database-before")
    store = OpsStore(settings.database_path)
    previous_sha = "b" * 40
    target_sha = "a" * 40
    previous = settings.releases_directory / previous_sha
    target = settings.releases_directory / target_sha
    previous.mkdir(parents=True)
    target.mkdir(parents=True)
    store.set("active_sha", previous_sha)
    manager = DeploymentManager(settings, store)
    deployment_id = store.create_deployment(target_sha, previous_sha, "checking", "test")
    events: list[str] = []
    active = [previous]
    monkeypatch.setattr(manager, "_active_release_path", lambda: active[0])
    monkeypatch.setattr(manager, "releases", lambda: [{"sha": target_sha, "deployable": True}])
    monkeypatch.setattr(manager, "_research_job_running", lambda: False)
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(manager, "_build_release", lambda sha: events.append("build") or target)
    monkeypatch.setattr(
        manager,
        "_preflight_migrations",
        lambda *args, **kwargs: events.append("preflight") or {"allowed": True},
    )
    monkeypatch.setattr(manager, "_run_service", lambda action, *args, **kwargs: events.append(action) or "ok")
    monkeypatch.setattr(manager, "_run_release_application", lambda *args, **kwargs: events.append("migrate") or "ok")
    def activate(release):
        events.append(f"activate:{release.name}")
        active[0] = release

    monkeypatch.setattr(manager, "_activate_release", activate)
    monkeypatch.setattr(manager, "_wait_healthy", lambda: (_ for _ in ()).throw(RuntimeError("unhealthy")))

    manager._deploy(deployment_id, target_sha, previous_sha, False)

    assert events.index("build") < events.index("preflight") < events.index("stop") < events.index("migrate")
    assert events.index("migrate") < events.index(f"activate:{target_sha}")
    assert active[0] == previous
    assert settings.research_database.read_bytes() == b"database-before"
    assert store.deployment(deployment_id)["status"] == "manual_intervention_required" or store.deployment(
        deployment_id
    )["status"] == "rolled_back"


def test_real_migration_failure_restores_backup_before_old_release_restart(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    settings.research_database.write_bytes(b"database-before")
    store = OpsStore(settings.database_path)
    previous_sha = "b" * 40
    target_sha = "a" * 40
    previous = settings.releases_directory / previous_sha
    target = settings.releases_directory / target_sha
    previous.mkdir(parents=True)
    target.mkdir(parents=True)
    store.set("active_sha", previous_sha)
    manager = DeploymentManager(settings, store)
    deployment_id = store.create_deployment(target_sha, previous_sha, "checking", "test")
    active = [previous]
    events: list[str] = []
    monkeypatch.setattr(manager, "_active_release_path", lambda: active[0])
    monkeypatch.setattr(manager, "releases", lambda: [{"sha": target_sha, "deployable": True}])
    monkeypatch.setattr(manager, "_research_job_running", lambda: False)
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: "")
    monkeypatch.setattr(manager, "_build_release", lambda sha: target)
    monkeypatch.setattr(manager, "_preflight_migrations", lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(manager, "_run_service", lambda action, *args, **kwargs: events.append(action) or "ok")

    def fail_migration(*args, **kwargs):
        settings.research_database.write_bytes(b"partially-migrated")
        raise RuntimeError("migration crashed")

    monkeypatch.setattr(manager, "_run_release_application", fail_migration)
    monkeypatch.setattr(manager, "_activate_release", lambda release: active.__setitem__(0, release))
    monkeypatch.setattr(manager, "_wait_healthy", lambda: None)

    manager._deploy(deployment_id, target_sha, previous_sha, False)

    assert active[0] == previous
    assert settings.research_database.read_bytes() == b"database-before"
    assert events.count("stop") == 2
    assert "start" in events
    assert store.deployment(deployment_id)["status"] == "rolled_back"


def test_release_contract_json_is_stable_and_complete():
    payload = migration_contract(MIGRATIONS)
    serialized = json.dumps(payload, sort_keys=True)

    assert json.loads(serialized) == payload


@pytest.mark.skipif(os.name == "nt", reason="Windows test account cannot create directory symlinks")
def test_active_release_switch_uses_one_atomic_symlink_replace(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    previous = settings.releases_directory / ("b" * 40)
    target = settings.releases_directory / ("a" * 40)
    previous.mkdir(parents=True)
    target.mkdir(parents=True)
    settings.current_release_link.symlink_to(previous, target_is_directory=True)
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observed_replace)

    manager._activate_release(target)

    assert settings.current_release_link.resolve() == target.resolve()
    assert len(replacements) == 1


def test_linux_runtime_contract_points_only_at_atomic_current_release():
    root = Path(__file__).parents[1]
    runtime_files = [
        root / "deploy" / "kfcquant-admin",
        root / "deploy" / "kfcquant-service-control",
        root / "deploy" / "systemd" / "kfcquant-worker.service",
        root / "deploy" / "systemd" / "kfcquant-web.service",
        root / "deploy" / "systemd" / "kfcops.service",
    ]

    for path in runtime_files:
        content = path.read_text(encoding="utf-8")
        assert "/opt/kfcquant/current" in content
        assert "/opt/kfcquant/app/.venv" not in content

    bootstrap = (root / "deploy" / "bootstrap_server.sh").read_text(encoding="utf-8")
    deploy = (root / "deploy" / "deploy_server.sh").read_text(encoding="utf-8")
    assert "git -C \"$REPOSITORY_DIR\" worktree add --detach" in bootstrap
    assert "RELEASES_DIR=/opt/kfcquant/releases" in bootstrap
    assert "CURRENT_RELEASE=/opt/kfcquant/current" in bootstrap
    assert "upsert_ops_setting KFCOPS_CURRENT_RELEASE_LINK" in bootstrap
    assert "--approve-irreversible-migration" in deploy


def test_repeated_release_build_reuses_only_a_complete_immutable_release(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    sha = "a" * 40
    release = settings.releases_directory / sha
    executable = release / ".venv" / ("Scripts/kfcquant.exe" if os.name == "nt" else "bin/kfcquant")
    executable.parent.mkdir(parents=True)
    executable.write_text("placeholder", encoding="utf-8")
    (release / ".release.env").write_text(f"KFCQUANT_SOURCE_SHA={sha}\n", encoding="utf-8")
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(manager, "_run_git", lambda *args, **kwargs: commands.append(args) or "")
    monkeypatch.setattr(manager, "_release_source_is_clean", lambda *args: True)

    assert manager._build_release(sha) == release
    assert commands == []

    (release / ".release.env").write_text("KFCQUANT_SOURCE_SHA=wrong\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        manager._build_release(sha)


def test_release_integrity_requires_expected_clean_git_head(tmp_path, monkeypatch):
    manager = DeploymentManager(m6_settings(tmp_path), OpsStore(tmp_path / "ops.sqlite3"))
    sha = "a" * 40
    outputs = iter([sha, ""])
    monkeypatch.setattr(manager, "_run_command", lambda *args, **kwargs: next(outputs))
    assert manager._release_source_is_clean(tmp_path, sha) is True

    outputs = iter([sha, " M src/kfcquant/cli.py"])
    monkeypatch.setattr(manager, "_run_command", lambda *args, **kwargs: next(outputs))
    assert manager._release_source_is_clean(tmp_path, sha) is False

    monkeypatch.setattr(manager, "_run_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git")))
    assert manager._release_source_is_clean(tmp_path, sha) is False


def test_release_build_creates_worktree_venv_and_manifest_before_publish(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    settings.repository_directory.mkdir()
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    sha = "a" * 40
    commands: list[tuple[object, ...]] = []

    def fake_git(*arguments, **kwargs):
        commands.append(arguments)
        if arguments[:3] == ("worktree", "add", "--detach"):
            staging = Path(arguments[3])
            staging.mkdir(parents=True)
            (staging / "requirements.lock").write_text("duckdb==1.3.0\n", encoding="utf-8")
        elif arguments[:2] == ("worktree", "move"):
            Path(arguments[2]).rename(Path(arguments[3]))
        elif arguments[0] == "show":
            return "2026-08-19T00:00:00+08:00"
        return ""

    def fake_command(command, **kwargs):
        commands.append(tuple(command))
        cwd = Path(kwargs["cwd"])
        if "venv" in command:
            python = cwd / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
        elif "--no-deps" in command:
            executable = cwd / ".venv" / ("Scripts/kfcquant.exe" if os.name == "nt" else "bin/kfcquant")
            executable.write_text("kfcquant", encoding="utf-8")
        return ""

    monkeypatch.setattr(manager, "_run_git", fake_git)
    monkeypatch.setattr(manager, "_run_command", fake_command)
    monkeypatch.setattr(manager, "_release_source_is_clean", lambda *args: True)

    release = manager._build_release(sha)

    assert release == settings.releases_directory / sha
    assert (release / ".release.env").read_text(encoding="utf-8").splitlines() == [
        f"KFCQUANT_SOURCE_SHA={sha}",
        "KFCQUANT_BUILD_TIME=2026-08-19T00:00:00+08:00",
    ]
    assert any(command[:2] == ("worktree", "move") for command in commands)
    assert any(command[-3:] == ("-m", "pip", "check") for command in commands)


def test_incomplete_release_build_is_removed_without_touching_published_release(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    sha = "a" * 40
    staging_paths: list[Path] = []
    removals: list[Path] = []

    def fake_git(*arguments, **kwargs):
        if arguments[:3] == ("worktree", "add", "--detach"):
            staging = Path(arguments[3])
            staging.mkdir(parents=True)
            staging_paths.append(staging)
        elif arguments[:3] == ("worktree", "remove", "--force"):
            removals.append(Path(arguments[3]))
        return ""

    monkeypatch.setattr(manager, "_run_git", fake_git)

    with pytest.raises(RuntimeError, match="requirements.lock"):
        manager._build_release(sha)

    assert removals == staging_paths
    assert not staging_paths[0].exists()
    assert not (settings.releases_directory / sha).exists()


def test_runtime_and_logs_are_resilient_and_redact_secrets(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))

    def service(action, service=None, **kwargs):
        if action == "logs":
            return "token=top-secret normal"
        if service == "web":
            raise RuntimeError("web unavailable")
        return "active"

    monkeypatch.setattr(manager, "_run_service", service)
    monkeypatch.setattr(manager, "_active_sha", lambda: "a" * 40)

    runtime = manager.runtime()

    assert runtime["active_sha"] == "a" * 40
    assert "kfcquant-worker: active" in runtime["service_status"]
    assert "web unavailable" in runtime["service_status"]
    assert manager.logs() == "token=[REDACTED] normal"

    monkeypatch.setattr(manager, "_run_service", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("denied")))
    assert "denied" in manager.logs()


def test_database_restore_is_atomic_and_discards_failed_migration_wal(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    backup = tmp_path / "backup.duckdb"
    backup.write_bytes(b"known-good")
    settings.research_database.write_bytes(b"partially-migrated")
    wal = settings.research_database.with_name(f"{settings.research_database.name}.wal")
    wal.write_bytes(b"failed-transaction")
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observed_replace)

    manager._restore_database_backup(backup)

    assert settings.research_database.read_bytes() == b"known-good"
    assert not wal.exists()
    assert len(replacements) == 1


def test_automatic_rollback_restores_an_initially_absent_database(tmp_path, monkeypatch):
    settings = m6_settings(tmp_path)
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    previous = settings.releases_directory / ("b" * 40)
    previous.mkdir(parents=True)
    settings.research_database.parent.mkdir(parents=True, exist_ok=True)
    settings.research_database.write_bytes(b"created-by-failed-release")
    settings.research_database.with_name(f"{settings.research_database.name}.wal").write_bytes(b"wal")
    monkeypatch.setattr(manager, "_run_service", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(manager, "_activate_release", lambda release: None)
    monkeypatch.setattr(manager, "_wait_healthy", lambda: None)

    manager._rollback(1, previous.name, previous, None, database_was_absent=True)

    assert not settings.research_database.exists()
    assert not settings.research_database.with_name(f"{settings.research_database.name}.wal").exists()
