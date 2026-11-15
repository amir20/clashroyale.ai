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
    chat_id: Annotated[
        str | None, typer.Argument(help="Show this chat in full (id or ref)")
    ] = None,
    tag: Annotated[str | None, typer.Option(help="Only this player's chats")] = None,
    failed: Annotated[bool, typer.Option(help="Only chats that ended in an error")] = False,
    disliked: Annotated[bool, typer.Option(help="Only answers rated thumbs down")] = False,
    kind: Annotated[str | None, typer.Option(help="question | preset | suggestions")] = None,
    limit: int = 20,
    full: Annotated[bool, typer.Option(help="Don't shorten tool results; show prompts")] = False,
) -> None:
    """Read back what people asked Rook and how it answered, tool calls included."""
    log = ChatLog(get_settings().db_path.with_name("app.db"))
    if chat_id is None:
        rows = log.recent(
            tag and normalize_tag(tag), failed=failed, disliked=disliked, kind=kind, limit=limit
        )
        _emit(rows, lambda: ui.show(ui.table(_compact(rows), "Rook chats, newest first")))
        return
    chat = log.get(int(chat_id) if chat_id.isdigit() else chat_id)
    if chat is None:
        raise typer.BadParameter(f"no chat {chat_id}")
    _emit(chat, lambda: _print_chat(chat, full))


def _compact(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One short line per chat; `crai chats ID` has the rest."""
    thumbs = {1: "👍", -1: "👎"}
    return [
        {
            "id": r["id"],
            "at": r["at"][5:16].replace("T", " "),
            "kind": r["kind"],
            "tag": r["tag"],
            "question": r["question"][:48],
            "result": r["outcome"],
            "rated": thumbs.get(r["rating"], ""),
            "seconds": r["seconds"],
        }
        for r in rows
    ]


def _shorten(text: str, full: bool) -> str:
    if full or len(text) <= TOOL_RESULT_CHARS:
        return text
    return text[:TOOL_RESULT_CHARS] + f"… ({len(text):,} chars)"


def _print_chat(chat: dict[str, Any], full: bool) -> None:
    c = ui.console
    c.print(
        f"[bold]#{chat['id']}[/] {chat['kind']} · {chat['at']} · {chat['tag']} · "
        f"{chat['outcome']} · {chat['model']} · {chat['rounds'] or 0} calls · "
        f"{chat['input_tokens'] or 0:,} in / {chat['output_tokens'] or 0:,} out · "
        f"${chat['cost_usd'] or 0:.4f} · {chat['seconds']}s"
    )
    c.print(
        f"[dim]ref {chat['ref']} · who {chat['who']} · prompt {chat['system_prompt']} · "
        f"tools {chat['tools']} · {chat['user_agent']}[/]",
        highlight=False,
    )
    if chat["rating"] is not None:
        thumb = "👍" if chat["rating"] > 0 else "👎"
        c.print(f"[bold]rated {thumb}[/] {chat['note'] or ''}")
    if full and chat.get("system_prompt_text"):
        c.print("\n[bold]system prompt:[/]")
        c.print(chat["system_prompt_text"], markup=False, highlight=False)
    for turn in chat["history"]:
        c.print(f"[dim]earlier {turn['role']}:[/] {turn['content']}")
    for m in chat["transcript"]:
        match m.get("role"):
            case "user":
                c.print(f"\n[bold cyan]user:[/] {_shorten(m['content'], full)}")
            case "assistant":
                if m.get("reasoning"):
                    c.print(f"  [dim italic]thinking: {_shorten(m['reasoning'], full)}[/]")
                if m.get("content"):
                    c.print(f"[bold green]rook:[/] {m['content']}")
                for call in m.get("tool_calls") or []:
                    fn = call["function"]
                    c.print(f"[yellow]→ {fn['name']}[/] {fn.get('arguments') or '{}'}")
            case "tool":
                c.print(f"  ← {_shorten(m['content'], full)}", markup=False, style="dim")
    if chat["steps"]:
        c.print("\n[bold]steps:[/]")
    for step in chat["steps"]:
        at = f"{step['at_ms'] / 1000:6.2f}s"
        if step["kind"] == "model":
            c.print(
                f"  {at}  model round {step['round']}: {step['ms']} ms, "
                f"{step['input_tokens']:,} in / {step['output_tokens']:,} out, "
                f"finish={step['finish']}"
            )
        else:
            error = f" [red]{step['error']}[/]" if step.get("error") else ""
            c.print(
                f"  {at}  {step['name']} {json.dumps(step['args'])}: {step['ms']} ms, "
                f"{step['bytes']:,} bytes, {step['rows']} rows{error}"
            )
    final = chat["final"] or {}
    c.print(f"\n[bold]shown:[/] {final.get('text') or final.get('line') or final.get('message')}")
    if final.get("visuals"):
        c.print(f"[dim]visuals: {json.dumps([v['type'] for v in final['visuals']])}[/]")
    if final.get("exception"):
        c.print(f"[red]exception: {final['exception']}[/]")
