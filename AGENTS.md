# AGENTS.md

Instructions for any coding agent working in this repo. `README.md` explains the product and the
data pipeline in depth; read its "How the data pipeline works" section before touching
`collect/`, `storage/` or `analysis/`. `src/crai/ml/README.md` covers the deck models.

## What this is

`crai` crawls Clash Royale battles from the official API into SQLite, copies deck rows to a
Parquet snapshot, and answers deck questions in polars. The same `Analyzer` serves three
front ends: the `crai` CLI, an MCP server (`crai mcp`), and **Rook**, a web app (FastAPI +
Svelte) where a crow coach explains a player's deck.

Pipeline: **crawl → store (SQLite) → snapshot (Parquet) → analyze**. Each stage reads only what
the previous one wrote, and every stage is safe to re-run.

## Commands

```sh
uv sync                                       # install (Python 3.15, uv fetches it)
uv run ruff format . && uv run ruff check .   # format + lint
uv run ty check                               # types (Astral's ty, not mypy/pyright)
uv run pytest -q                              # tests, no network
uv run crai dev                               # API on :8011 + Vite on :5173
cd web && bun run check                       # Svelte/TS type check
```

CI (`.github/workflows/ci.yml`) runs format check, lint, ty and pytest. All four must pass
before you call a change done.

## Layout

```
src/crai/
  collect/    API client, BFS crawler, API payload TypedDicts
  storage/    SQLite schema + writes, Parquet snapshot, DB upgrades
  analysis/   analytics.py (deck stats), insights.py (one player), visuals.py
  coach/      Rook: voice.py (scripted lines), agent.py (LLM loop), tools.py, prompts/*.txt
  web/        app.py (routes only), loading.py (SSE status), service.py (reports, limits)
  ml/         deck-vs-deck win model: export, train (torch, Mac only), publish, explain
  cli/        typer commands
  mcp_server.py, config.py
web/          Svelte 5 + Tailwind 4 + daisyUI, built with bun/Vite
tests/        pytest; conftest.py builds synthetic API payloads and a populated DB
```

## Code conventions

- **Python 3.15, used on purpose.** `lazy import` (PEP 810) for heavy modules (polars, httpx,
  uvicorn) so the CLI starts fast; `frozendict`; PEP 649 deferred annotations (no
  `from __future__ import annotations`, no `TYPE_CHECKING` blocks); `except A, B` without
  parentheses; PEP 695 `type` aliases and generics. Exception: aliases typer reads at runtime
  stay plain assignments.
- Ruff config in `pyproject.toml` is strict (line length 100, ANN, PL, PTH, PERF...). Fix the
  code rather than adding `noqa`; if an ignore is really needed, give the reason inline.
- Type everything. `Any` is fine only for raw JSON at the boundary.
- polars for analytics, never pandas. Stay lazy/columnar; don't `.to_dicts()` or explode big
  frames. The production deck table is ~22M rows, so a careless copy or explode has OOM-killed
  the server before. Filter and narrow first, then join or aggregate.
- SQL is built from fixed fragments plus bound parameters. Never interpolate user input.
- `web/app.py` holds routes only; logic goes in `service`, `loading` or `coach`.
- Rook's prompts live in `coach/prompts/*.txt` and reload without a restart. Edit them there,
  not in Python strings.
- No `print` in library code; use `logging` (CLI output goes through `cli/ui.py` / rich).

## Data rules that are easy to break

- Battles are stored once: the same battle from both players' logs (timestamps up to 10 s
  apart) dedups to one row, and side 0 is the player whose tag sorts first. Writes are
  insert-or-ignore and must stay idempotent.
- `deck_key` is the 8 card IDs sorted and joined with `-`.
- Changing the SQLite schema means bumping `SCHEMA_VERSION` in `storage/schema.py` and adding
  an upgrade path. Changing snapshot columns means bumping `FORMAT` in `storage/snapshot.py`.
- Stats default to the last 7 days, collection decks, excluding friendlies, clanmate and boat
  battles. A draw counts as half a win.
- Every win rate shown to a user carries a `margin` (95% MoE) and a games count. Don't present
  a difference smaller than the margin as real. Rook must never claim something the battle
  data doesn't support (`tests/test_coach_accuracy.py` guards this).
- Card levels are shown on the in-game scale (16 = max); the API reports them per rarity.

## Tests

- No network, ever. HTTP goes through `httpx.MockTransport`; the LLM is faked.
- Use the fixtures in `tests/conftest.py` (card IDs like `HOG`, `PEKKA`, decks `CYCLE`,
  `BEATDOWN`, player `ME`) instead of inventing new data shapes.
- Warnings are errors (`filterwarnings = ["error"]`).
- When you fix a bug, add a test that fails without the fix.

## Environment

- Secrets live in `.env` (`CR_API_TOKEN`, `CR_MY_TAG`, `OPENROUTER_API_KEY`); see
  `.env.example`. Never commit `.env` or anything under `data/`.
- The local `data/clash.db` may be missing or tiny. Production data (~28 GB DB) lives on the
  deploy host, and laptop timings understate it by 10–20×. Don't make performance claims from
  local runs alone.
- torch/numpy are in the optional `ml` group. CI and the server image don't install them, so
  only `ml/model.py`, `ml/train.py` and `ml/publish.py` may import torch.

## Git

- Commit straight to `main` and push; this project doesn't use PRs.
- Commit messages: a plain-English subject saying what changed for the user, then a short body
  saying why and how, with real numbers when it's a perf or accuracy change. No `feat:` prefixes.

## Writing style

Docs, UI copy, commit messages and Rook's lines use plain language: short sentences, concrete
numbers, no jargon or marketing words. Match the tone of `README.md`.
