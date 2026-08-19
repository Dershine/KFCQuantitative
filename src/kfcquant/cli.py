from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kfcquant.config import SHANGHAI_TZ, get_settings
from kfcquant.observability import configure_observability
from kfcquant.runtime import health_info, version_info
from kfcquant.scheduler import run_scheduler
from kfcquant.services.workflow import Workflow

app = typer.Typer(help="A股双时段机会研究 Agent（仅研究与影子组合）", no_args_is_help=True)
console = Console()


def _workflow() -> Workflow:
    settings = get_settings()
    return Workflow(settings, observability=configure_observability(settings))


def _date(value: str | None, default: date) -> date:
    return date.fromisoformat(value) if value else default


@app.command()
def doctor(online: bool = typer.Option(False, help="同时验证外部接口；会消耗少量配额")) -> None:
    table = Table("检查项", "状态", "详情")
    checks = _workflow().doctor(online=online)
    for item in checks:
        table.add_row(str(item["check"]), "OK" if item["ok"] else "FAIL", str(item["detail"]))
    console.print(table)
    if any(not item["ok"] for item in checks):
        raise typer.Exit(code=1)


@app.command("sync-eod")
def sync_eod(
    start: str | None = typer.Option(None, help="YYYY-MM-DD；首次建议至少向前180个自然日"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD；默认今天"),
) -> None:
    today = datetime.now(SHANGHAI_TZ).date()
    end_date = _date(end, today)
    start_date = _date(start, end_date)
    result = _workflow().sync_eod(start_date, end_date)
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@app.command("run-preclose")
def run_preclose(
    research_outside_window: bool = typer.Option(False, "--research-only", help="允许窗口外研究，但绝不生成订单"),
) -> None:
    run = _workflow().run_preclose(research_outside_window=research_outside_window)
    console.print_json(run.model_dump_json())


@app.command("run-morning")
def run_morning(
    research_outside_window: bool = typer.Option(False, "--research-only", help="允许窗口外研究，不创建订单"),
) -> None:
    run = _workflow().run_morning(research_outside_window=research_outside_window)
    console.print_json(run.model_dump_json())


@app.command("sync-calendar")
def sync_calendar() -> None:
    console.print_json(json.dumps(_workflow().sync_calendar(), ensure_ascii=False, default=str))


@app.command("evaluate-morning")
def evaluate_morning() -> None:
    results = _workflow().evaluate_morning()
    console.print_json(json.dumps([item.model_dump(mode="json") for item in results], ensure_ascii=False))


@app.command()
def migrate() -> None:
    workflow = _workflow()
    console.print(f"schema version: {workflow.database.migration_version()}")


@app.command()
def health(as_json: bool = typer.Option(False, "--json", help="输出机器可读JSON")) -> None:
    settings = get_settings()
    payload = health_info(settings, configure_observability(settings))
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        console.print(payload)
    if payload["status"] != "ok":
        raise typer.Exit(code=1)


@app.command("version")
def show_version(as_json: bool = typer.Option(False, "--json", help="输出机器可读JSON")) -> None:
    payload = version_info(get_settings())
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        console.print(payload)


@app.command()
def scheduler() -> None:
    run_scheduler(get_settings())


@app.command("schedule-plan")
def schedule_plan(as_json: bool = typer.Option(False, "--json", help="输出机器可读JSON")) -> None:
    payload = get_settings().schedule.registration_plan()
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        console.print(payload)


@app.command("capture-fill")
def capture_fill() -> None:
    fills = _workflow().capture_fill()
    console.print_json(json.dumps([fill.model_dump(mode="json") for fill in fills], ensure_ascii=False))


@app.command("monitor-paper")
def monitor_paper() -> None:
    fills = _workflow().monitor_paper()
    console.print_json(json.dumps([fill.model_dump(mode="json") for fill in fills], ensure_ascii=False))


@app.command("run-postclose")
def run_postclose() -> None:
    console.print(_workflow().run_postclose())


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址；生产环境建议仅监听本机"),
    port: int = typer.Option(8501, help="监听端口"),
) -> None:
    dashboard = Path(__file__).with_name("dashboard.py")
    settings = get_settings()
    arguments = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard),
        f"--server.address={host}",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    base_path = getattr(settings, "base_url_path", "")
    if base_path:
        arguments.append(f"--server.baseUrlPath={base_path.strip('/')}")
    raise typer.Exit(subprocess.call(arguments))


if __name__ == "__main__":
    app()
