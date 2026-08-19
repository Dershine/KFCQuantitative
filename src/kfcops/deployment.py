from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import duckdb
import httpx
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from kfcops.config import OpsSettings
from kfcops.store import OpsStore
from kfcops.supply_chain import verify_release_manifest, write_release_manifest

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATES = {"succeeded", "rolled_back", "failed", "manual_intervention_required"}


class DeploymentManager:
    """Deploy tested Git commits into a native virtualenv managed by systemd."""

    def __init__(self, settings: OpsSettings, store: OpsStore):
        self.settings = settings
        self.store = store
        self._mutex = threading.Lock()

    def releases(self) -> list[dict[str, object]]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        with httpx.Client(timeout=15, headers=headers) as client:
            commits = client.get(
                f"https://api.github.com/repos/{self.settings.github_repository}/commits",
                params={"sha": "main", "per_page": 15},
            )
            commits.raise_for_status()
            runs = client.get(
                f"https://api.github.com/repos/{self.settings.github_repository}/actions/runs",
                params={"branch": "main", "status": "completed", "per_page": 50},
            )
            runs.raise_for_status()
        successful = {
            item["head_sha"]: {
                "id": item.get("id"),
                "url": item.get("html_url", ""),
                "name": item.get("name", "test-and-release"),
                "conclusion": "success",
            }
            for item in runs.json().get("workflow_runs", [])
            if item.get("name") == "test-and-release" and item.get("conclusion") == "success"
        }
        return [
            {
                "sha": item["sha"],
                "short_sha": item["sha"][:12],
                "message": item["commit"]["message"].splitlines()[0],
                "date": item["commit"]["committer"]["date"],
                "deployable": item["sha"] in successful,
                "workflow": successful.get(item["sha"]),
            }
            for item in commits.json()
        ]

    def request_deploy(self, sha: str, *, approve_irreversible: bool = False) -> tuple[str, int]:
        self._validate_sha(sha)
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        current = self._active_sha()
        if self._protected_window():
            deployment_id = self.store.create_deployment(sha, current, "pending", "交易窗口内，仅记录待处理请求")
            self.store.set("pending_sha", sha)
            self.store.set("pending_approve_irreversible", "yes" if approve_irreversible else "")
            self.store.audit(
                "deploy",
                sha,
                "pending",
                f"protected trading window; irreversible_approval={approve_irreversible}",
            )
            return "pending", deployment_id
        deployment_id = self.store.create_deployment(sha, current, "checking", "开始检查发布版本")
        threading.Thread(
            target=self._deploy,
            args=(deployment_id, sha, current, approve_irreversible),
            daemon=True,
        ).start()
        return "checking", deployment_id

    def deploy_now(self, sha: str, *, approve_irreversible: bool = False) -> int:
        """Run a deployment synchronously for the root-operated deployment script."""
        self._validate_sha(sha)
        if self._protected_window():
            raise RuntimeError("交易保护窗口内禁止部署")
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        current = self._active_sha()
        deployment_id = self.store.create_deployment(sha, current, "checking", "开始检查发布版本")
        self._deploy(deployment_id, sha, current, approve_irreversible)
        deployment = self.store.deployment(deployment_id)
        status = str(deployment["status"]) if deployment else "missing"
        if status != "succeeded":
            raise RuntimeError(f"部署未成功，最终状态: {status}")
        return deployment_id

    def request_rollback(self) -> int:
        previous = self.store.get("previous_sha")
        if not previous:
            raise RuntimeError("没有可回滚的上一版本")
        if self._protected_window():
            raise RuntimeError("交易窗口内禁止回滚")
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        deployment_id = self.store.create_deployment(previous, self._active_sha(), "rolling_back", "手动回滚")
        backup_value = self.store.get("previous_backup")
        backup = Path(backup_value) if backup_value else None
        threading.Thread(target=self._rollback_exclusive, args=(deployment_id, previous, backup), daemon=True).start()
        return deployment_id

    def restart(self) -> None:
        if self._protected_window() or self._busy():
            raise RuntimeError("当前禁止重启")
        self._run_service("restart")
        self._wait_healthy()
        self.store.audit("restart", self._active_sha(), "success", "research services restarted")

    def cancel_pending(self) -> None:
        sha = self.store.get("pending_sha")
        self.store.set("pending_sha", "")
        self.store.set("pending_approve_irreversible", "")
        self.store.audit("cancel-pending", sha, "success", "pending deployment cleared")

    def runtime(self) -> dict[str, object]:
        states = []
        for service in ("worker", "web"):
            try:
                state = self._run_service("is-active", service, check=False).strip()
            except Exception as exc:
                state = str(exc)
            states.append(f"kfcquant-{service}: {state}")
        disk = shutil.disk_usage(self.settings.research_database.parent)
        certificate: dict[str, object] = {"configured": False}
        if self.settings.certificate_path and self.settings.certificate_path.exists():
            try:
                decoded = ssl._ssl._test_decode_cert(str(self.settings.certificate_path))  # noqa: SLF001
                certificate = {"configured": True, "expires_at": decoded.get("notAfter", "unknown")}
            except Exception as exc:
                certificate = {"configured": True, "error": str(exc)}
        return {
            "active_sha": self._active_sha(),
            "previous_sha": self.store.get("previous_sha"),
            "pending_sha": self.store.get("pending_sha"),
            "service_status": "\n".join(states),
            "disk_free_bytes": disk.free,
            "certificate": certificate,
        }

    def logs(self) -> str:
        try:
            return self._redact(self._run_service("logs", check=False))
        except Exception as exc:
            return f"日志暂不可用: {exc}"

    def _deploy(
        self,
        deployment_id: int,
        sha: str,
        previous: str,
        approve_irreversible: bool = False,
    ) -> None:
        if not self._mutex.acquire(blocking=False):
            self._stage(deployment_id, "failed", "另一个部署线程已在执行")
            return
        deployment_lock = FileLock(self.settings.deployment_lock)
        try:
            deployment_lock.acquire(timeout=0)
        except FileLockTimeout:
            self._stage(deployment_id, "failed", "另一个部署进程已在执行")
            self._mutex.release()
            return
        backup: Path | None = None
        previous_release = self._active_release_path()
        target_release: Path | None = None
        activated = False
        rollback_ready = False
        try:
            self._stage(deployment_id, "checking", "验证GitHub Actions状态")
            release = next((item for item in self.releases() if item["sha"] == sha), None)
            if not release or not release["deployable"]:
                raise RuntimeError("该提交没有成功的main工作流，禁止部署")
            self._stage(deployment_id, "prechecking", "检查任务和磁盘")
            if self._research_job_running():
                raise RuntimeError("研究任务正在运行")
            self._stage(deployment_id, "fetching", "从Git远端拉取目标提交")
            self._run_git("fetch", "--prune", "origin", "main")
            self._run_git("cat-file", "-e", f"{sha}^{{commit}}")
            self._stage(deployment_id, "building", "在独立Release目录构建目标版本")
            target_release = self._build_release(sha, workflow=release.get("workflow"))
            self._stage(deployment_id, "prechecking_migrations", "在数据库副本上预检迁移与回滚兼容性")
            compatibility = self._preflight_migrations(
                previous_release,
                target_release,
                approve_irreversible=approve_irreversible,
            )
            self.store.audit(
                "migration-preflight",
                sha,
                "approved" if compatibility.get("approval_recorded") else "success",
                json.dumps(compatibility, ensure_ascii=False, sort_keys=True),
            )
            self._stage(deployment_id, "backing_up", "停止服务并创建部署前数据库备份")
            self._run_service("stop")
            backup = self._backup_database(sha)
            rollback_ready = True
            self._stage(deployment_id, "migrating", "使用目标Release执行正式数据库迁移")
            self._run_release_application(target_release, "migrate")
            self._stage(deployment_id, "switching", "原子切换Active Release")
            self._activate_release(target_release)
            activated = True
            self._stage(deployment_id, "starting", "启动新的Active Release")
            self._run_service("start")
            self._stage(deployment_id, "healthchecking", "等待网页和worker健康")
            self._wait_healthy()
            self.store.set("previous_sha", previous)
            self.store.set("previous_backup", str(backup) if backup else "")
            self.store.set("active_sha", sha)
            self.store.set("pending_sha", "")
            self._stage(deployment_id, "succeeded", "部署成功")
            self.store.audit("deploy", sha, "success", f"previous={previous}")
            self._prune_backups()
            self._run_service("restart-ops", check=False)
        except Exception as exc:
            self._stage(deployment_id, "failed", str(exc))
            if rollback_ready and previous and previous_release is not None:
                self._stage(deployment_id, "rolling_back", "自动恢复上一版本")
                self._rollback(
                    deployment_id,
                    previous,
                    previous_release,
                    backup,
                    database_was_absent=backup is None,
                )
            elif not activated:
                # Build and preflight happen before service stop and Active Release switching.
                self.store.audit("deploy", sha, "failed", f"active release unchanged: {exc}")
            else:
                self._run_service("start", check=False)
                self._stage(deployment_id, "manual_intervention_required", "没有上一版本可自动回滚")
        finally:
            deployment_lock.release()
            self._mutex.release()

    def _rollback(
        self,
        deployment_id: int,
        sha: str,
        release: Path,
        backup: Path | None,
        *,
        database_was_absent: bool = False,
    ) -> None:
        try:
            self._run_service("stop", check=False)
            if database_was_absent:
                self._restore_absent_database_state()
            elif self.settings.research_database.exists() and (backup is None or not backup.exists()):
                raise RuntimeError("数据库存在但部署前备份缺失，拒绝启动旧Release")
            elif backup and backup.exists():
                self._restore_database_backup(backup)
            self._activate_release(release)
            self._run_service("start")
            self._wait_healthy()
            self.store.set("active_sha", sha)
            self.store.set("previous_sha", "")
            self.store.set("previous_backup", "")
            self._stage(deployment_id, "rolled_back", f"已恢复版本 {sha[:12]}")
            self.store.audit("rollback", sha, "success", "service healthy")
            self._run_service("restart-ops", check=False)
        except Exception as exc:
            self._stage(deployment_id, "manual_intervention_required", f"回滚失败: {exc}")
            self.store.audit("rollback", sha, "failed", str(exc))

    def _rollback_exclusive(self, deployment_id: int, sha: str, backup: Path | None) -> None:
        if not self._mutex.acquire(blocking=False):
            self._stage(deployment_id, "failed", "另一个部署线程已在执行")
            return
        deployment_lock = FileLock(self.settings.deployment_lock)
        try:
            deployment_lock.acquire(timeout=0)
        except FileLockTimeout:
            self._stage(deployment_id, "failed", "另一个部署进程已在执行")
            self._mutex.release()
            return
        try:
            release = self.settings.releases_directory / sha
            if not self._valid_release(release, sha):
                raise RuntimeError(f"上一Release不可用: {release}")
            self._rollback(deployment_id, sha, release, backup)
        except Exception as exc:
            self._stage(deployment_id, "manual_intervention_required", f"回滚预检失败: {exc}")
            self.store.audit("rollback", sha, "failed", str(exc))
        finally:
            deployment_lock.release()
            self._mutex.release()

    def _active_sha(self) -> str:
        recorded = self.store.get("active_sha")
        if recorded:
            return recorded
        release = self._active_release_path()
        if release is not None:
            return release.name
        try:
            return self._run_git("rev-parse", "HEAD").strip()
        except Exception:
            return ""

    def _active_release_path(self) -> Path | None:
        link = self.settings.current_release_link
        if not link.exists():
            return None
        try:
            release = link.resolve(strict=True)
            releases_root = self.settings.releases_directory.resolve(strict=False)
            if release.parent != releases_root or not SHA_PATTERN.fullmatch(release.name):
                return None
            return release
        except (OSError, RuntimeError):
            return None

    def _validate_sha(self, sha: str) -> None:
        if not SHA_PATTERN.fullmatch(sha):
            raise ValueError("版本必须是40位小写Git SHA")

    def _protected_window(self) -> bool:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        if now.weekday() >= 5:
            return False
        is_open = True
        if self.settings.research_database.exists():
            try:
                with FileLock(self.settings.research_lock, timeout=5):
                    with duckdb.connect(str(self.settings.research_database), read_only=True) as connection:
                        row = connection.execute(
                            "SELECT is_open FROM trade_calendar WHERE cal_date=?", [now.date()]
                        ).fetchone()
                        is_open = bool(row and row[0])
            except Exception:
                is_open = True
        return is_open and self.settings.protected_window_start <= now.time() <= self.settings.protected_window_end

    def _research_job_running(self) -> bool:
        if not self.settings.research_database.exists():
            return False
        try:
            with FileLock(self.settings.research_lock, timeout=5):
                with duckdb.connect(str(self.settings.research_database), read_only=True) as connection:
                    row = connection.execute(
                        """SELECT 1
                           FROM job_runs LEFT JOIN job_leases USING (job_run_id)
                           WHERE status='running'
                             AND (job_leases.job_run_id IS NULL OR lease_expires_at >= current_timestamp)
                           LIMIT 1"""
                    ).fetchone()
                    return row is not None
        except Exception:
            return True

    def _busy(self) -> bool:
        recent = self.store.recent_deployments(1)
        return bool(recent and recent[0]["status"] not in TERMINAL_STATES | {"pending"})

    def _build_release(self, sha: str, *, workflow: object = None) -> Path:
        self.settings.releases_directory.mkdir(parents=True, exist_ok=True)
        release = self.settings.releases_directory / sha
        if release.exists():
            if self._valid_release(release, sha):
                return release
            raise RuntimeError(f"Release目录已存在但不完整，拒绝覆盖: {release}")

        staging = self.settings.releases_directory / f".{sha}.building-{uuid4().hex}"
        moved = False
        try:
            self._run_git("worktree", "add", "--detach", str(staging), sha)
            lock_file = staging / "requirements.lock"
            if not lock_file.exists():
                raise RuntimeError("目标版本缺少 requirements.lock")
            # A virtualenv is not relocatable on POSIX: console-script shebangs
            # contain the absolute environment path.  Move the detached source
            # worktree to its immutable Release path before creating the venv.
            # The Release is still unpublished until the atomic `current` link
            # switch, and every failure below removes this incomplete directory.
            self._run_git("worktree", "move", str(staging), str(release))
            moved = True
            lock_file = release / "requirements.lock"
            self._run_command(
                [str(self.settings.builder_python), "-m", "venv", str(release / ".venv")],
                cwd=release,
                timeout=900,
            )
            python = self._python_executable(release)
            self._run_command(
                [str(python), "-m", "pip", "install", "--requirement", str(lock_file)],
                cwd=release,
                timeout=1800,
            )
            self._run_command(
                [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", str(release)],
                cwd=release,
                timeout=900,
            )
            self._run_command([str(python), "-m", "pip", "check"], cwd=release, timeout=300)
            if not isinstance(workflow, dict):
                raise RuntimeError("目标版本缺少成功工作流来源证据")
            self._write_release(release, sha, workflow=workflow)
            validation_errors = self._release_validation_errors(release, sha)
            if validation_errors:
                raise RuntimeError(
                    f"Release构建后完整性检查失败: {release}: "
                    + "; ".join(validation_errors)
                )
            return release
        except Exception:
            candidate = release if moved else staging
            self._run_git("worktree", "remove", "--force", str(candidate), check=False)
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            raise

    def _write_release(self, release: Path, sha: str, *, workflow: dict[str, object]) -> None:
        build_time = self._run_git("show", "-s", "--format=%cI", sha).strip()
        manifest = write_release_manifest(
            release,
            sha,
            source_commit_time=build_time,
            workflow=workflow,
            run_command=lambda command: self._run_command(command, cwd=release, timeout=300),
        )
        release_env_file = release / ".release.env"
        temporary = release / ".release.env.tmp"
        temporary.write_text(
            f"KFCQUANT_SOURCE_SHA={sha}\n"
            f"KFCQUANT_BUILD_TIME={build_time}\n"
            "KFCQUANT_RELEASE_MANIFEST=.release-manifest.json\n"
            f"KFCQUANT_DEPENDENCY_LOCK_SHA256={manifest['requirements_lock_sha256']}\n",
            encoding="utf-8",
        )
        temporary.replace(release_env_file)

    def _valid_release(self, release: Path, sha: str) -> bool:
        return not self._release_validation_errors(release, sha)

    def _release_validation_errors(self, release: Path, sha: str) -> list[str]:
        errors: list[str] = []
        if release.name != sha or not SHA_PATTERN.fullmatch(sha):
            errors.append("release path or source SHA is invalid")
        env_file = release / ".release.env"
        try:
            values = dict(
                line.split("=", 1)
                for line in env_file.read_text(encoding="utf-8").splitlines()
                if line and "=" in line
            )
        except OSError:
            errors.append("release environment is unreadable")
            return errors
        if values.get("KFCQUANT_SOURCE_SHA") != sha:
            errors.append("release environment source SHA mismatch")
        if not self._application_executable(release).is_file():
            errors.append("release application executable is missing")
        if not self._release_source_is_clean(release, sha):
            errors.append("release Git source is dirty or at the wrong commit")
        manifest_name = values.get("KFCQUANT_RELEASE_MANIFEST")
        if manifest_name is None:
            return errors  # Compatibility for the Active Release built before M6-B.
        if manifest_name != ".release-manifest.json":
            errors.append("release manifest path is invalid")
        try:
            requirements_lock_sha256 = hashlib.sha256(
                (release / "requirements.lock").read_bytes()
            ).hexdigest()
        except OSError:
            errors.append("requirements lock is unreadable")
            return errors
        lock_sha256 = values.get("KFCQUANT_DEPENDENCY_LOCK_SHA256")
        if not lock_sha256 or not SHA256_PATTERN.fullmatch(lock_sha256):
            errors.append("dependency lock hash is missing or malformed")
        elif lock_sha256 != requirements_lock_sha256:
            errors.append("dependency lock hash mismatch")
        if not verify_release_manifest(release, sha):
            errors.append("release provenance manifest verification failed")
        return errors

    def _release_source_is_clean(self, release: Path, sha: str) -> bool:
        try:
            head = self._run_command(
                ["git", "-C", str(release), "rev-parse", "HEAD"],
                cwd=release,
                timeout=60,
                stdout_only=True,
            ).strip()
            changes = self._run_command(
                ["git", "-C", str(release), "status", "--porcelain", "--untracked-files=no"],
                cwd=release,
                timeout=60,
                stdout_only=True,
            ).strip()
            return head == sha and not changes
        except Exception:
            return False

    def _activate_release(self, release: Path) -> None:
        release = release.resolve(strict=True)
        link = self.settings.current_release_link
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.{uuid4().hex}.tmp")
        try:
            temporary.symlink_to(release, target_is_directory=True)
            os.replace(temporary, link)
        finally:
            if temporary.is_symlink():
                temporary.unlink()

    @staticmethod
    def _validate_contract(contract: dict[str, object]) -> tuple[int, list[dict[str, object]]]:
        if contract.get("contract_version") != 1:
            raise RuntimeError("unsupported migration contract version")
        latest = contract.get("latest_schema_version")
        migrations = contract.get("migrations")
        if not isinstance(latest, int) or latest < 0 or not isinstance(migrations, list):
            raise RuntimeError("invalid migration contract")
        versions = [item.get("version") for item in migrations if isinstance(item, dict)]
        if len(versions) != len(migrations) or versions != list(range(1, latest + 1)):
            raise RuntimeError("migration contract versions must be consecutive")
        if any(
            not SHA256_PATTERN.fullmatch(str(item.get("statements_sha256", "")))
            for item in migrations
        ):
            raise RuntimeError("migration contract requires a statement hash for every version")
        return latest, migrations

    def _release_contract(self, release: Path) -> dict[str, object]:
        output = self._run_release_application(release, "migration-contract", "--json")
        try:
            contract = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Release迁移契约不是有效JSON: {release}") from exc
        if not isinstance(contract, dict):
            raise RuntimeError(f"Release迁移契约格式无效: {release}")
        self._validate_contract(contract)
        return contract

    def _assess_migration_compatibility(
        self,
        active_contract: dict[str, object],
        target_contract: dict[str, object],
        database_version: int,
        approve_irreversible: bool,
    ) -> dict[str, object]:
        active_latest, active_migrations = self._validate_contract(active_contract)
        target_latest, target_migrations = self._validate_contract(target_contract)
        result: dict[str, object] = {
            "allowed": False,
            "database_version": database_version,
            "active_release_schema_version": active_latest,
            "target_release_schema_version": target_latest,
            "pending_versions": [],
            "rollback_strategy": "blocked",
            "approval_required": False,
            "approval_recorded": False,
            "reason": "",
        }
        if database_version > active_latest:
            result["reason"] = "database is newer than the Active Release contract"
            return result
        if database_version > target_latest:
            result["reason"] = "target release would downgrade the database schema"
            return result
        applied_versions = min(database_version, active_latest, target_latest)
        for index in range(applied_versions):
            active = active_migrations[index]
            target = target_migrations[index]
            if active["name"] != target["name"] or active["statements_sha256"] != target["statements_sha256"]:
                result["reason"] = f"applied migration contract drift at version {index + 1}"
                return result

        pending = [item for item in target_migrations if int(item["version"]) > database_version]
        result["pending_versions"] = [int(item["version"]) for item in pending]
        valid_policies = {"in_place", "backup_restore", "requires_approval"}
        policies = {str(item.get("rollback_policy", "")) for item in pending}
        if not policies <= valid_policies or any(not str(item.get("rollback_reason", "")).strip() for item in pending):
            result["reason"] = "target migration has missing or unknown rollback policy"
            return result
        approval_required = "requires_approval" in policies
        result["approval_required"] = approval_required
        if approval_required and not approve_irreversible:
            result["reason"] = "target contains an irreversible migration that requires explicit approval"
            return result

        result["allowed"] = True
        result["approval_recorded"] = approval_required and approve_irreversible
        if not pending:
            result["rollback_strategy"] = "no_schema_change"
        elif policies == {"in_place"}:
            result["rollback_strategy"] = "in_place"
        else:
            result["rollback_strategy"] = "restore_deployment_backup"
        result["reason"] = "migration compatibility accepted"
        return result

    def _database_schema_version(self, database: Path) -> int:
        if not database.exists():
            return 0
        with duckdb.connect(str(database), read_only=True) as connection:
            table = connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)

    def _preflight_migrations(
        self,
        active_release: Path | None,
        target_release: Path,
        *,
        approve_irreversible: bool,
    ) -> dict[str, object]:
        if active_release is None:
            raise RuntimeError("Active Release链接无效，无法验证回滚兼容性")
        active_contract = self._release_contract(active_release)
        target_contract = self._release_contract(target_release)
        database_version = self._database_schema_version(self.settings.research_database)
        matrix = self._assess_migration_compatibility(
            active_contract,
            target_contract,
            database_version,
            approve_irreversible,
        )
        if not matrix["allowed"]:
            raise RuntimeError(f"迁移兼容预检失败: {matrix['reason']}")

        self.settings.backup_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="migration-preflight-", dir=self.settings.backup_directory
        ) as directory:
            database_copy = Path(directory) / "research.duckdb"
            if self.settings.research_database.exists():
                try:
                    with FileLock(self.settings.research_lock, timeout=5):
                        shutil.copy2(self.settings.research_database, database_copy)
                except FileLockTimeout as exc:
                    raise RuntimeError("无法取得研究数据库锁以创建迁移预检副本") from exc
            self._run_release_application(target_release, "migrate", "--database", str(database_copy))
            migrated_version = self._database_schema_version(database_copy)
            if migrated_version != matrix["target_release_schema_version"]:
                raise RuntimeError(
                    "迁移预检后的Schema版本与目标Release契约不一致: "
                    f"{migrated_version} != {matrix['target_release_schema_version']}"
                )
        matrix["copy_migration_verified"] = True
        return matrix

    def _backup_database(self, sha: str) -> Path | None:
        if not self.settings.research_database.exists():
            return None
        self.settings.backup_directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(ZoneInfo(self.settings.timezone))
        target = self.settings.backup_directory / f"{now:%Y%m%d%H%M%S}-{sha[:12]}.duckdb"
        shutil.copy2(self.settings.research_database, target)
        return target

    def _restore_database_backup(self, backup: Path) -> None:
        database = self.settings.research_database
        database.parent.mkdir(parents=True, exist_ok=True)
        temporary = database.with_name(f".{database.name}.{uuid4().hex}.restore")
        try:
            shutil.copy2(backup, temporary)
            os.replace(temporary, database)
            wal = database.with_name(f"{database.name}.wal")
            if wal.exists():
                wal.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()

    def _restore_absent_database_state(self) -> None:
        database = self.settings.research_database
        for path in (database, database.with_name(f"{database.name}.wal")):
            if path.exists():
                path.unlink()

    def _prune_backups(self) -> None:
        backups = sorted(
            self.settings.backup_directory.glob("*.duckdb"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in backups[self.settings.backup_retention :]:
            path.unlink()

    def _wait_healthy(self) -> None:
        last_error = ""
        for _ in range(24):
            try:
                response = httpx.get(self.settings.research_health_url, timeout=5)
                worker_active = self._run_service("is-active", "worker", check=False).strip() == "active"
                health_output = self._run_application(
                    "health",
                    "--json",
                    check=False,
                    stdout_only=True,
                )
                health = json.loads(health_output)
                if response.status_code == 200 and worker_active and health.get("status") == "ok":
                    return
                last_error = f"web={response.status_code}; worker={worker_active}; health={health_output[-500:]}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(5)
        raise RuntimeError(f"健康检查超时: {last_error}")

    def _python_executable(self, release: Path) -> Path:
        executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        path = release / ".venv" / executable
        if not path.exists():
            raise RuntimeError(f"Release虚拟环境不存在: {path}")
        return path

    def _application_executable(self, release: Path) -> Path:
        executable = "Scripts/kfcquant.exe" if os.name == "nt" else "bin/kfcquant"
        return release / ".venv" / executable

    def _run_git(self, *arguments: str, check: bool = True) -> str:
        return self._run_command(
            ["git", "-C", str(self.settings.repository_directory), *arguments],
            check=check,
            timeout=900,
        )

    def _run_application(
        self,
        *arguments: str,
        check: bool = True,
        stdout_only: bool = False,
    ) -> str:
        if os.name == "nt":
            release = self._active_release_path()
            if release is None:
                raise RuntimeError("Active Release链接无效")
            executable = self._application_executable(release)
            return self._run_command(
                [str(executable), *arguments],
                check=check,
                timeout=900,
                stdout_only=stdout_only,
            )
        return self._run_service("app", *arguments, check=check, stdout_only=stdout_only)

    def _run_release_application(self, release: Path, *arguments: str, check: bool = True) -> str:
        executable = self._application_executable(release)
        if not executable.exists():
            raise RuntimeError(f"Release应用入口不存在: {executable}")
        return self._run_command([str(executable), *arguments], cwd=release, check=check, timeout=900)

    def _run_service(
        self,
        *arguments: str,
        check: bool = True,
        stdout_only: bool = False,
    ) -> str:
        command = ["sudo", "-n", str(self.settings.service_control_command), *arguments]
        return self._run_command(command, check=check, timeout=120, stdout_only=stdout_only)

    def _run_command(
        self,
        command: list[str],
        *,
        check: bool = True,
        timeout: int = 900,
        cwd: Path | None = None,
        stdout_only: bool = False,
    ) -> str:
        result = subprocess.run(
            command,
            cwd=cwd or self.settings.repository_directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = self._redact(f"{result.stdout}\n{result.stderr}")
        if check and result.returncode != 0:
            raise RuntimeError(output[-4000:])
        return self._redact(result.stdout) if stdout_only else output

    @staticmethod
    def _redact(value: str) -> str:
        return re.sub(r"(?i)(token|key|password|secret)=\S+", r"\1=[REDACTED]", value)

    def _stage(self, deployment_id: int, status: str, message: str) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone)).isoformat()
        self.store.update_deployment(deployment_id, status, message, f"[{now}] {status}: {message}\n")
