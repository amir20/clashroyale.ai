# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- **Answer Clash Royale questions with data.** The `clash-royale` MCP server (`.mcp.json`, runs
  `uv run crai mcp`) exposes the same analytics as the CLI. Use it, or `uv run crai ...`, for
  deck and card questions instead of answering from memory. Always mention games, margin and
  the time window used.
- **After changing MCP tools**, restart the MCP server (`/mcp`) before testing them; the running
  process still has the old code.
- **After a UI change** in `web/`, run `uv run crai dev`, screenshot the page with headless
  Playwright (it's already a dev dependency in `web/`) at phone width, and show it before saying
  you're done. Rook is used mostly on phones.
- **Before committing**, run the four CI checks from AGENTS.md. Then commit on `main` and push.
- **Production** runs in Docker on a NAS, reached through Rex; logs and CPU/memory are in
  Dozzle. Test performance there in a throwaway container, never inside the live one. Don't
  restart or redeploy production without asking.
