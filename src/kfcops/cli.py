from __future__ import annotations

import typer
import uvicorn

from kfcops.config import OpsSettings

app = typer.Typer(help="KFCQuant运行管理器")


@app.callback()
def main() -> None:
    """KFCQuant运行管理器命令行。"""


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8600) -> None:
    settings = OpsSettings()
    if settings.session_secret == "change-me":
        raise typer.BadParameter("KFCOPS_SESSION_SECRET must be configured")
    uvicorn.run("kfcops.web:create_app", host=host, port=port, factory=True, proxy_headers=True)


if __name__ == "__main__":
    app()
