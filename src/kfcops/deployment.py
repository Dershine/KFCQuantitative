from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import httpx
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from kfcops.config import OpsSettings
from kfcops.store import OpsStore

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
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
            item["head_sha"]
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
            }
            for item in commits.json()
        ]

    def request_deploy(self, sha: str) -> tuple[str, int]:
        self._validate_sha(sha)
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        current = self._active_sha()
        if self._protected_window():
            deployment_id = self.store.create_deployment(sha, current, "pending", "交易窗口内，仅记录待处理请求")
            self.store.set("pending_sha", sha)
            self.store.audit("deploy", sha, "pending", "protected trading window")
            return "pending", deployment_id
        deployment_id = self.store.create_deployment(sha, current, "checking", "开始检查发布版本")
        threading.Thread(target=self._deploy, args=(deployment_id, sha, current), daemon=True).start()
        return "checking", deployment_id

    def deploy_now(self, sha: str) -> int:
        """Run a deployment synchronously for the root-operated deployment script."""
        self._validate_sha(sha)
        if self._protected_window():
            raise RuntimeError("交易保护窗口内禁止部署")
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        current = self._active_sha()
        deployment_id = self.store.create_deployment(sha, current, "checking", "开始检查发布版本")
        self._deploy(deployment_id, sha, current)
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

    def _deploy(self, deployment_id: int, sha: str, previous: str) -> None:
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
            self._stage(deployment_id, "backing_up", "停止服务并备份数据库")
            self._run_service("stop")
            backup = self._backup_database(sha)
            self._stage(deployment_id, "installing", "检出源码并安装锁定依赖")
            self._checkout_and_install(sha)
            self._write_release(sha)
            self._stage(deployment_id, "migrating", "执行数据库迁移")
            self._run_application("migrate")
            self._stage(deployment_id, "starting", "启动新版本")
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
            if previous:
                self._stage(deployment_id, "rolling_back", "自动恢复上一版本")
                self._rollback(deployment_id, previous, backup)
            else:
                self._run_service("start", check=False)
                self._stage(deployment_id, "manual_intervention_required", "没有上一版本可自动回滚")
        finally:
            deployment_lock.release()
            self._mutex.release()

    def _rollback(self, deployment_id: int, sha: str, backup: Path | None) -> None:
        try:
            self._run_service("stop", check=False)
            if backup and backup.exists():
                shutil.copy2(backup, self.settings.research_database)
            self._checkout_and_install(sha)
            self._write_release(sha)
            self._run_application("migrate")
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
            self._rollback(deployment_id, sha, backup)
        finally:
            deployment_lock.release()
            self._mutex.release()

    def _active_sha(self) -> str:
        recorded = self.store.get("active_sha")
        if recorded:
            return recorded
        try:
            return self._run_git("rev-parse", "HEAD").strip()
        except Exception:
            return ""

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
                    row = connection.execute("SELECT 1 FROM job_runs WHERE status='running' LIMIT 1").fetchone()
                    return row is not None
        except Exception:
            return True

    def _busy(self) -> bool:
        recent = self.store.recent_deployments(1)
        return bool(recent and recent[0]["status"] not in TERMINAL_STATES | {"pending"})

    def _checkout_and_install(self, sha: str) -> None:
        self._run_git("checkout", "--detach", sha)
        lock_file = self.settings.repository_directory / "requirements.lock"
        if not lock_file.exists():
            raise RuntimeError("目标版本缺少 requirements.lock")
        python = self._python_executable()
        self._run_command([str(python), "-m", "pip", "install", "--requirement", str(lock_file)], timeout=1800)
        self._run_command(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-build-isolation",
                "--no-deps",
                str(self.settings.repository_directory),
            ],
            timeout=900,
        )

    def _write_release(self, sha: str) -> None:
        build_time = self._run_git("show", "-s", "--format=%cI", sha).strip()
        self.settings.release_env_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.release_env_file.with_suffix(".tmp")
        temporary.write_text(
            f"KFCQUANT_SOURCE_SHA={sha}\nKFCQUANT_BUILD_TIME={build_time}\n",
            encoding="utf-8",
        )
        temporary.replace(self.settings.release_env_file)

    def _backup_database(self, sha: str) -> Path | None:
        if not self.settings.research_database.exists():
            return None
        self.settings.backup_directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(ZoneInfo(self.settings.timezone))
        target = self.settings.backup_directory / f"{now:%Y%m%d%H%M%S}-{sha[:12]}.duckdb"
        shutil.copy2(self.settings.research_database, target)
        return target

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
                health_output = self._run_application("health", "--json", check=False)
                health = json.loads(health_output)
                if response.status_code == 200 and worker_active and health.get("status") == "ok":
                    return
                last_error = f"web={response.status_code}; worker={worker_active}; health={health_output[-500:]}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(5)
        raise RuntimeError(f"健康检查超时: {last_error}")

    def _python_executable(self) -> Path:
        executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        path = self.settings.virtualenv_directory / executable
        if not path.exists():
            raise RuntimeError(f"生产虚拟环境不存在: {path}")
        return path

    def _run_git(self, *arguments: str, check: bool = True) -> str:
        return self._run_command(
            ["git", "-C", str(self.settings.repository_directory), *arguments],
            check=check,
            timeout=900,
        )

    def _run_application(self, *arguments: str, check: bool = True) -> str:
        if os.name == "nt":
            executable = self.settings.virtualenv_directory / "Scripts/kfcquant.exe"
            return self._run_command([str(executable), *arguments], check=check, timeout=900)
        return self._run_service("app", *arguments, check=check)

    def _run_service(self, *arguments: str, check: bool = True) -> str:
        command = ["sudo", "-n", str(self.settings.service_control_command), *arguments]
        return self._run_command(command, check=check, timeout=120)

    def _run_command(self, command: list[str], *, check: bool = True, timeout: int = 900) -> str:
        result = subprocess.run(
            command,
            cwd=self.settings.repository_directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = self._redact(f"{result.stdout}\n{result.stderr}")
        if check and result.returncode != 0:
            raise RuntimeError(output[-4000:])
        return output

    @staticmethod
    def _redact(value: str) -> str:
        return re.sub(r"(?i)(token|key|password|secret)=\S+", r"\1=[REDACTED]", value)

    def _stage(self, deployment_id: int, status: str, message: str) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone)).isoformat()
        self.store.update_deployment(deployment_id, status, message, f"[{now}] {status}: {message}\n")
