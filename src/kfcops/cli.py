from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from kfcops.assurance import AssuranceManager
from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore
from kfcops.supply_chain import write_release_manifest

app = typer.Typer(help="KFCQuant运行管理器")


@app.callback()
def main() -> None:
    """KFCQuant运行管理器命令行。"""


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8600) -> None:
    OpsSettings()  # Validate before handing control to the server factory.
    uvicorn.run("kfcops.web:create_app", host=host, port=port, factory=True, proxy_headers=True)


@app.command()
def deploy(
    sha: str = typer.Argument(..., help="通过main工作流的40位Git提交SHA"),
    approve_irreversible_migration: bool = typer.Option(
        False,
        "--approve-irreversible-migration",
        help="显式批准迁移契约中标记为不可逆的变更",
    ),
) -> None:
    """Synchronously deploy one tested commit; intended for deploy_server.sh."""
    settings = OpsSettings()
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    deployment_id = manager.deploy_now(sha, approve_irreversible=approve_irreversible_migration)
    typer.echo(f"deployment {deployment_id} succeeded")


@app.command("recovery-drill")
def recovery_drill(
    backup: Annotated[
        Path | None,
        typer.Option("--backup", help="指定backup目录内的DuckDB备份；默认使用最新备份"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出机器可读JSON")] = False,
) -> None:
    """Restore a retained backup into an isolated copy and record its health evidence."""
    settings = OpsSettings()
    report = AssuranceManager(settings, OpsStore(settings.database_path)).run_recovery_drill(backup)
    typer.echo(json.dumps(report, ensure_ascii=False, default=str) if as_json else report["report_path"])
    if report["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("capacity-baseline")
def capacity_baseline(
    query_samples: Annotated[int | None, typer.Option("--query-samples", min=1)] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出机器可读JSON")] = False,
) -> None:
    """Collect read-only runtime, lock, query, storage, and recovery evidence."""
    settings = OpsSettings()
    report = AssuranceManager(settings, OpsStore(settings.database_path)).collect_capacity_baseline(
        query_samples=query_samples
    )
    typer.echo(json.dumps(report, ensure_ascii=False, default=str) if as_json else report["report_path"])
    if report["status"] != "complete":
        raise typer.Exit(code=1)


@app.command("capacity-decision")
def capacity_decision(
    baseline: Annotated[Path, typer.Argument(help="capacity-baselines目录中的JSON报告")],
    multiple_writers_required: Annotated[bool, typer.Option("--multiple-writers-required")] = False,
    remote_transactions_required: Annotated[bool, typer.Option("--remote-transactions-required")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="输出机器可读JSON")] = False,
) -> None:
    """Apply typed expansion thresholds without changing the active architecture."""
    settings = OpsSettings()
    root = (settings.assurance_directory / "capacity-baselines").resolve(strict=False)
    selected = baseline.resolve(strict=True)
    try:
        selected.relative_to(root)
    except ValueError as exc:
        raise typer.BadParameter("baseline must be inside the configured capacity-baselines directory") from exc
    payload = json.loads(selected.read_text(encoding="utf-8"))
    report = AssuranceManager(settings, OpsStore(settings.database_path)).evaluate_capacity(
        payload,
        multiple_writers_required=multiple_writers_required,
        remote_transactions_required=remote_transactions_required,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, default=str) if as_json else report["report_path"])


@app.command("write-release-manifest", hidden=True)
def write_release_provenance(release: Path, sha: str, source_commit_time: str) -> None:
    """Create initial bootstrap provenance using the installed Release environment."""
    if not release.is_dir():
        raise typer.BadParameter("release directory does not exist")
    manifest = write_release_manifest(
        release,
        sha,
        source_commit_time=source_commit_time,
        workflow={"id": "initial-bootstrap", "url": "", "name": "bootstrap", "conclusion": "bootstrap"},
    )
    typer.echo(manifest["manifest_sha256"])


if __name__ == "__main__":
    app()
