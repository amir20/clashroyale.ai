# crai: Clash Royale deck analytics + MCP

This tool crawls battle logs from the top Path of Legend players, from you, from everyone they fought and from everyone *those* players fought. It stores the logs in SQLite, then answers card and deck questions with polars. You can run those questions from the CLI or as an MCP server for Claude.

## Setup

Requires Python 3.15 (uv installs it automatically).

```sh
uv sync
cp .env.example .env   # then fill in CR_API_TOKEN and CR_MY_TAG
```

To get a token, create a key at <https://developer.clashroyale.com> and whitelist your **public IP** on it (`curl https://api.ipify.org`). If your IP changes often, you can use the RoyaleAPI proxy: whitelist `45.79.218.79` on the key and set `CR_API_BASE=https://proxy.royaleapi.dev/v1`.

## Crawl (repeatable)

```sh
uv run crai crawl                       # top 200 + you, depth 2, at most 3000 players fetched
uv run crai crawl --top 1000 --max-players 10000
uv run crai crawl --top 0 --depth 0     # just refresh yourself
```

- Every write is an upsert. A battle appears in both players' logs, sometimes with timestamps a second apart, so battles with the same participants within 10 seconds are stored once.
- A player whose log was fetched in the last `--refresh-hours` (default 12) is skipped. Their opponents already in the DB are still used to expand the crawl.
- The API only returns about the last 30 battles per player. Re-run it on a schedule (cron or launchd) to build up history.
- Each run is logged in the `crawl_runs` table.

## Ask questions (CLI)

```sh
uv run crai overview
uv run crai pairs "hog rider"              # what pairs with Hog (sort: games|adj_win_rate|lift|synergy)
uv run crai counters "x-bow"               # opponent cards that beat X-Bow decks
uv run crai decks --with pekka --without "mega knight" --type pathOfLegend
uv run crai complete hog log "ice spirit"  # finish a partial deck
uv run crai stats --sort adj_win_rate --top-only
uv run crai player                         # you (CR_MY_TAG)
uv run crai my-decks --min-level 15        # proven decks you can build at your levels
uv run crai sql "select battle_type, count(*) from battles group by 1"
```

Card names are fuzzy-matched (`pekka`, `xbow`, `mk`, `log`).

## MCP

By default the server runs over **stdio**: Claude Code launches `crai mcp` as a subprocess, so there's no port and nothing to keep running. `.mcp.json` registers it for this directory. To use it elsewhere:

```sh
claude mcp add clash-royale -- uv run --directory /Users/amirraminfar/Workspace/clashroyale.ai crai mcp
```

For a long-running **HTTP** server instead: `uv run crai mcp --transport streamable-http --port 8765`, which serves `http://127.0.0.1:8765/mcp`. Every tool declares an output schema (from the TypedDicts in `analytics.py`).

Tools: `db_overview`, `find_card`, `list_cards`, `card_stats`, `card_pairs`, `card_matchups`, `compare_cards`, `top_decks`, `complete_deck`, `player_profile`, `player_cards`, `decks_for_player`, `refresh_player` (live API), `get_schema`, `run_sql` (read-only).

## How the stats work

- The unit is one player's 8-card deck in one 1v1 battle. By default this covers `collection` decks and excludes friendly, clanmate and boat battles. Filter with `battle_types` / `--type`, `since_days` / `--since`, and `top_only` / `--top-only` (only decks played by seed players).
- `win_rate` counts a draw as half a win. `adj_win_rate` = (wins + 10) / (games + 20), which shrinks small samples toward 50%.
- **Pairs**: `pair_rate` = P(partner | card). `lift` = pair_rate / partner's overall use rate. `synergy` = win rate of decks with both cards minus the card's own win rate.
- **Matchups**: the win rate of decks *with* the card when the opponent has card Y.
- `level_normalized` puts levels on the in-game scale (the API reports levels per rarity).

## Layout

```
src/crai/models.py      TypedDicts for the API payloads
src/crai/api.py         async httpx client (retries, 429 backoff, concurrency limit)
src/crai/db.py          schema + idempotent ingest
src/crai/crawl.py       BFS crawler
src/crai/analytics.py   polars analytics
src/crai/cli.py         typer CLI
src/crai/mcp_server.py  MCP server
```

## Development

```sh
uv run ruff format . && uv run ruff check .   # lint + format
uv run ty check                               # types (Astral's ty)
uv run pytest -q                              # tests: no network (httpx.MockTransport)
```

CI runs the same commands (`.github/workflows/ci.yml`).

**Zed**: `.zed/settings.json` uses ty + ruff as the Python language servers (basedpyright and pyright are disabled), with format, import sorting and fixes on save. `.zed/tasks.json` has tasks to run checks, tests (whole file or the symbol under the cursor), crawl, and the HTTP MCP server. Open them with `task: spawn`.

**Python 3.15 features used**: `lazy import` (PEP 810) keeps polars/httpx out of commands that don't need them (CLI import drops from ~175 ms to ~23 ms), `frozendict` (PEP 814), unpacking in comprehensions (PEP 798). From 3.14: deferred annotations (PEP 649, so there's no `from __future__` or `TYPE_CHECKING`) and unparenthesized `except A, B` (PEP 758). Also PEP 695 generics and `type` aliases. The aliases that typer reads at runtime stay plain assignments, because typer can't unwrap `TypeAliasType` yet.
