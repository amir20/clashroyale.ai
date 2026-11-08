"""Commands that run servers: MCP, the web app, and local development."""

import os
import subprocess
from typing import Annotated

import typer
lazy import uvicorn

from crai.cli.app import (
    app,
)
from crai.config import PROJECT_ROOT
lazy from crai.mcp_server import run as run_mcp


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | streamable-http")] = "stdio",
    port: int = 8765,
) -> None:
    """Run the MCP server (stdio by default)."""
    run_mcp(transport, port)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Restart on code changes")] = False,
) -> None:
    """Run the Rook web app (API plus the built Svelte UI)."""
    uvicorn.run("crai.web:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def dev(port: int = 8011, web_port: int = 5173) -> None:
    """Develop locally: hot reload, no rate limits, AI usage printed in the page."""
    os.environ["CRAI_DEV"] = "1"  # inherited by uvicorn's reload workers
    command = ["bun", "run", "dev", "--host", "--port", str(web_port), "--strictPort"]
    web = subprocess.Popen(command, cwd=PROJECT_ROOT / "web")
    try:
        uvicorn.run(
            "crai.web:create_app",
            factory=True,
            host="127.0.0.1",
            port=port,
            reload=True,
            reload_dirs=[str(PROJECT_ROOT / "src" / "crai")],
        )
    finally:
        web.terminate()
