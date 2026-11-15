"""Commands that run servers: MCP, the web app, and local development."""

import json
import os
import subprocess
from typing import Annotated, Any

import typer
lazy import uvicorn

from crai.cli.app import (
    _emit,
    app,
)
from crai.config import PROJECT_ROOT, get_settings
lazy from crai.cli import ui
lazy from crai.coach.chats import ChatLog
lazy from crai.collect.client import normalize_tag
lazy from crai.mcp_server import run as run_mcp

TOOL_RESULT_CHARS = 600


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


@app.command()
def chats(
    chat_id: Annotated[int | None, typer.Argument(help="Show this chat in full")] = None,
    tag: Annotated[str | None, typer.Option(help="Only this player's chats")] = None,
    failed: Annotated[bool, typer.Option(help="Only chats that ended in an error")] = False,
    limit: int = 20,
    full: Annotated[bool, typer.Option(help="Don't shorten tool results")] = False,
) -> None:
    """Read back what people asked Rook and how it answered, tool calls included."""
    log = ChatLog(get_settings().db_path.with_name("app.db"))
    if chat_id is None:
        rows = log.recent(tag and normalize_tag(tag), failed=failed, limit=limit)
        _emit(rows, lambda: ui.show(ui.table(rows, "Rook chats, newest first")))
        return
    chat = log.get(chat_id)
    if chat is None:
        raise typer.BadParameter(f"no chat {chat_id}")
    _emit(chat, lambda: _print_chat(chat, full))


def _print_chat(chat: dict[str, Any], full: bool) -> None:
    c = ui.console
    c.print(
        f"[bold]#{chat['id']}[/] {chat['at']} {chat['tag']} · {chat['outcome']} · "
        f"{chat['model']} · ${chat['cost_usd'] or 0:.4f} · {chat['seconds']}s"
    )
    for turn in chat["history"]:
        c.print(f"[dim]earlier {turn['role']}:[/] {turn['content']}")
    for m in chat["transcript"]:
        match m.get("role"):
            case "user":
                c.print(f"\n[bold cyan]user:[/] {m['content']}")
            case "assistant":
                if m.get("content"):
                    c.print(f"[bold green]rook:[/] {m['content']}")
                for call in m.get("tool_calls") or []:
                    fn = call["function"]
                    c.print(f"[yellow]→ {fn['name']}[/] {fn.get('arguments') or '{}'}")
            case "tool":
                text = m["content"]
                if not full and len(text) > TOOL_RESULT_CHARS:
                    text = text[:TOOL_RESULT_CHARS] + f"… ({len(m['content'])} chars)"
                c.print(f"  ← {text}", markup=False, style="dim")
    final = chat["final"] or {}
    c.print(f"\n[bold]shown:[/] {final.get('text') or final.get('message')}")
    if final.get("visuals"):
        c.print(f"[dim]visuals: {json.dumps([v['type'] for v in final['visuals']])}[/]")
    if final.get("exception"):
        c.print(f"[red]exception: {final['exception']}[/]")
