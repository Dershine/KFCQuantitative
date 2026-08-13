from __future__ import annotations

import re
import shutil
import ssl
import subprocess
import threading
import time
from datetime import datetime
from datetime import time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import httpx
from filelock import FileLock

from kfcops.config import OpsSettings
from kfcops.store import OpsStore

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATES = {"succeeded", "rolled_back", "manual_intervention_required"}


class DeploymentManager:
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
            item["head_sha"] for item in runs.json().get("workflow_runs", []) if item.get("conclusion") == "success"
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
        current = self.store.get("active_sha")
        if self._protected_window():
            deployment_id = self.store.create_deployment(sha, current, "pending", "交易窗口内，仅记录待处理请求")
            self.store.set("pending_sha", sha)
            self.store.audit("deploy", sha, "pending", "protected trading window")
            return "pending", deployment_id
        deployment_id = self.store.create_deployment(sha, current, "checking", "开始检查发布版本")
        thread = threading.Thread(target=self._deploy, args=(deployment_id, sha, current), daemon=True)
        thread.start()
        return "checking", deployment_id

    def request_rollback(self) -> int:
        previous = self.store.get("previous_sha")
        if not previous:
            raise RuntimeError("没有可回滚的上一版本")
        if self._protected_window():
            raise RuntimeError("交易窗口内禁止回滚")
        if self._busy():
            raise RuntimeError("已有部署正在执行")
        deployment_id = self.store.create_deployment(previous, self.store.get("active_sha"), "rolling_back", "手动回滚")
        backup_value = self.store.get("previous_backup")
        backup = Path(backup_value) if backup_value else None
        threading.Thread(target=self._rollback, args=(deployment_id, previous, backup), daemon=True).start()
        return deployment_id

    def restart(self) -> None:
        if self._protected_window() or self._busy():
            raise RuntimeError("当前禁止重启")
        self._run_compose("restart", "research-web", "research-worker")
        self.store.audit("restart", self.store.get("active_sha"), "success", "research services restarted")

    def cancel_pending(self) -> None:
        sha = self.store.get("pending_sha")
        self.store.set("pending_sha", "")
        self.store.audit("cancel-pending", sha, "success", "pending deployment cleared")

    def runtime(self) -> dict[str, object]:
        try:
            output = self._run_compose("ps", "--format", "json", check=False)
        except Exception as exc:
            output = str(exc)
        disk = shutil.disk_usage(self.settings.research_database.parent)
        certificate: dict[str, object] = {"configured": False}
        if self.settings.certificate_path and self.settings.certificate_path.exists():
            try:
                decoded = ssl._ssl._test_decode_cert(str(self.settings.certificate_path))  # noqa: SLF001
                certificate = {"configured": True, "expires_at": decoded.get("notAfter", "unknown")}
            except Exception as exc:
                certificate = {"configured": True, "error": str(exc)}
        return {
            "active_sha": self.store.get("active_sha"),
            "previous_sha": self.store.get("previous_sha"),
            "pending_sha": self.store.get("pending_sha"),
            "compose_ps": output[-8000:],
            "disk_free_bytes": disk.free,
            "certificate": certificate,
        }

    def logs(self) -> str:
        try:
            return self._redact(
                self._run_compose("logs", "--tail", "200", "research-web", "research-worker", check=False)
            )
        except Exception as exc:
            return f"日志暂不可用: {exc}"

    def _deploy(self, deployment_id: int, sha: str, previous: str) -> None:
        if not self._mutex.acquire(blocking=False):
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
            self._stage(deployment_id, "pulling", "拉取不可变镜像")
            self._write_release(sha)
            self._run_compose("pull", "research-web", "research-worker")
            self._stage(deployment_id, "backing_up", "停止服务并备份数据库")
            self._run_compose("stop", "research-web", "research-worker")
            backup = self._backup_database(sha)
            self._stage(deployment_id, "migrating", "执行数据库迁移")
            self._run_compose("run", "--rm", "research-worker", "kfcquant", "migrate")
            self._stage(deployment_id, "starting", "启动新版本")
            self._run_compose("up", "-d", "--remove-orphans")
            self._stage(deployment_id, "healthchecking", "等待网页和worker健康")
            self._wait_healthy()
            self.store.set("previous_sha", previous)
            self.store.set("previous_backup", str(backup) if backup else "")
            self.store.set("active_sha", sha)
            self.store.set("pending_sha", "")
            self._stage(deployment_id, "succeeded", "部署成功")
            self.store.audit("deploy", sha, "success", f"previous={previous}")
            self._prune_backups()
        except Exception as exc:
            self._stage(deployment_id, "failed", str(exc))
            if previous:
                self._stage(deployment_id, "rolling_back", "自动恢复上一版本")
                self._rollback(deployment_id, previous, backup)
            else:
                self._stage(deployment_id, "manual_intervention_required", "没有上一版本可自动回滚")
        finally:
            self._mutex.release()

    def _rollback(self, deployment_id: int, sha: str, backup: Path | None) -> None:
        try:
            self._run_compose("stop", "research-web", "research-worker")
            if backup and backup.exists():
                shutil.copy2(backup, self.settings.research_database)
            self._write_release(sha)
            self._run_compose("up", "-d", "--remove-orphans")
            self._wait_healthy()
            current = self.store.get("active_sha")
            self.store.set("active_sha", sha)
            self.store.set("previous_sha", current if current != sha else "")
            self._stage(deployment_id, "rolled_back", f"已恢复版本 {sha[:12]}")
            self.store.audit("rollback", sha, "success", "service healthy")
        except Exception as exc:
            self._stage(deployment_id, "manual_intervention_required", f"回滚失败: {exc}")
            self.store.audit("rollback", sha, "failed", str(exc))

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
        return is_open and wall_time(8, 15) <= now.time() <= wall_time(15, 10)

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

    def _write_release(self, sha: str) -> None:
        self.settings.release_env_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.release_env_file.with_suffix(".tmp")
        temporary.write_text(f"KFCQUANT_IMAGE_TAG=sha-{sha}\n", encoding="utf-8")
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
                worker = self._run_compose("exec", "-T", "research-worker", "kfcquant", "health", "--json", check=False)
                if response.status_code == 200 and '"status": "ok"' in worker:
                    return
                last_error = f"web={response.status_code}; worker={worker[-500:]}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(5)
        raise RuntimeError(f"健康检查超时: {last_error}")

    def _run_compose(self, *arguments: str, check: bool = True) -> str:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.settings.release_env_file),
            "-f",
            str(self.settings.compose_file),
            *arguments,
        ]
        result = subprocess.run(
            command,
            cwd=self.settings.compose_directory,
            capture_output=True,
            text=True,
            timeout=900,
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
