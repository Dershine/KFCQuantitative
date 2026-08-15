from __future__ import annotations

import typer
import uvicorn

from kfcops.config import OpsSettings
from kfcops.deployment import DeploymentManager
from kfcops.store import OpsStore

app = typer.Typer(help="KFCQuant运行管理器")


@app.callback()
def main() -> None:
    """KFCQuant运行管理器命令行。"""


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8600) -> None:
    OpsSettings()  # Validate before handing control to the server factory.
    uvicorn.run("kfcops.web:create_app", host=host, port=port, factory=True, proxy_headers=True)


@app.command()
def deploy(sha: str = typer.Argument(..., help="通过main工作流的40位Git提交SHA")) -> None:
    """Synchronously deploy one tested commit; intended for deploy_server.sh."""
    settings = OpsSettings()
    manager = DeploymentManager(settings, OpsStore(settings.database_path))
    deployment_id = manager.deploy_now(sha)
    typer.echo(f"deployment {deployment_id} succeeded")


if __name__ == "__main__":
    app()
