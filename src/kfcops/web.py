from __future__ import annotations

import secrets
from pathlib import Path

import duckdb
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from filelock import FileLock
from starlette.middleware.sessions import SessionMiddleware

from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore


def create_app(settings: OpsSettings | None = None) -> FastAPI:
    settings = settings or OpsSettings()
    store = OpsStore(settings.database_path)
    manager = DeploymentManager(settings, store)
    app = FastAPI(title="KFCQuant Operations", docs_url=None, redoc_url=None)
    app.state.manager = manager
    app.state.store = store
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="strict", https_only=True)
    templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return str(token)

    def verify(request: Request, token: str, confirm: str) -> None:
        if not secrets.compare_digest(str(request.session.get("csrf", "")), token):
            raise HTTPException(403, "CSRF validation failed")
        if confirm != "yes":
            raise HTTPException(400, "operation must be explicitly confirmed")

    @app.get("/ops/", response_class=HTMLResponse)
    def dashboard(request: Request):
        error = ""
        try:
            releases = manager.releases()
        except Exception as exc:
            releases = []
            error = f"读取GitHub发布状态失败: {exc}"
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "csrf": csrf_token(request),
                "runtime": manager.runtime(),
                "research": research_summary(settings.research_database, settings.research_lock),
                "releases": releases,
                "deployments": store.recent_deployments(),
                "audit": store.recent_audit(),
                "logs": manager.logs(),
                "error": error,
            },
        )

    @app.get("/ops/health")
    def health():
        return {"status": "ok"}

    @app.post("/ops/actions/deploy/{sha}")
    def deploy(request: Request, sha: str, csrf: str = Form(...), confirm: str = Form(...)):
        verify(request, csrf, confirm)
        try:
            manager.request_deploy(sha)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/ops/", status_code=303)

    @app.post("/ops/actions/rollback")
    def rollback(request: Request, csrf: str = Form(...), confirm: str = Form(...)):
        verify(request, csrf, confirm)
        try:
            manager.request_rollback()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/ops/", status_code=303)

    @app.post("/ops/actions/restart")
    def restart(request: Request, csrf: str = Form(...), confirm: str = Form(...)):
        verify(request, csrf, confirm)
        try:
            manager.restart()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/ops/", status_code=303)

    @app.post("/ops/actions/cancel-pending")
    def cancel_pending(request: Request, csrf: str = Form(...), confirm: str = Form(...)):
        verify(request, csrf, confirm)
        manager.cancel_pending()
        return RedirectResponse("/ops/", status_code=303)

    return app


def research_summary(path: Path, lock_path: Path) -> dict[str, object]:
    result: dict[str, object] = {"database": str(path), "ok": False, "signals": []}
    if not path.exists():
        result["message"] = "研究数据库尚未创建"
        return result
    try:
        with FileLock(lock_path, timeout=5):
            with duckdb.connect(str(path), read_only=True) as connection:
                result["signals"] = [
                    dict(zip([item[0] for item in connection.description], row, strict=True))
                    for row in connection.execute(
                        """SELECT signal_kind,as_of,status,candidate_count,message FROM signal_runs
                           QUALIFY row_number() OVER(PARTITION BY signal_kind ORDER BY as_of DESC)=1"""
                    ).fetchall()
                ]
                result["job"] = connection.execute(
                    "SELECT job_name,started_at,status,message FROM job_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
        result["ok"] = True
    except Exception as exc:
        result["message"] = str(exc)
    return result
