# REVIEW.md

What to look for when reviewing a change to this repo. Conventions are in `AGENTS.md`; this file
is about what's most likely to go wrong. Report real problems with a concrete failure case.
Skip style nits that ruff or ty would already catch.

## High priority

**Memory and speed on real data.** Production has ~22M deck rows on a slow CPU with a 16 GB
container limit, and careless polars code has OOM-killed it before. Flag:
- copying or filtering the full deck table when it could narrow to the relevant decks first
- `explode`, cross joins or per-card row expansion over a large frame
- `.to_dicts()`, `.rows()` or Python loops over big frames
- caching a second full copy of a table
- work done per request that could be computed once at warm-up

**Statistical honesty.** This product's promise is that every claim is backed by data.
- Win rates shown or compared without games count and `margin`.
- A difference smaller than the margin presented as a recommendation.
- Small samples ranked by raw `win_rate` instead of `adj_win_rate`.
- Results from few players presented as general.
- Wrong default filters: last 7 days, collection decks, no friendlies/clanmate/boat battles,
  draws as half a win.
- Comparing groups that don't match: ranked (Path of Legend) battles have no trophies, so
  "players at your trophies" must not mix ranked and ladder.
- Rook (prompts, `voice.py`, tool outputs) saying anything the numbers don't support.

**Data integrity.**
- Writes that aren't idempotent or bypass battle dedup and side ordering.
- Schema changes without a `SCHEMA_VERSION` bump and upgrade path; snapshot column changes
  without a `FORMAT` bump.
- Anything that could write a half-finished Parquet part or skip the snapshot lock.
- Card levels not converted to the in-game scale (16 = max).

**Security.**
- User input reaching SQL other than as a bound parameter (`run_sql` must stay read-only).
- Secrets from `.env` logged, returned by an API, or committed.
- Web endpoints missing the rate or daily limits in `web/service.py`, especially ones that
  call the LLM or the Clash Royale API.

## Medium priority

- **Tests:** new behavior or a bug fix without a test; tests that hit the network; tests that
  build ad-hoc data instead of using `tests/conftest.py`.
- **Import cost:** a heavy module (polars, httpx, torch, uvicorn) imported eagerly where
  `lazy import` would do; torch imported outside `ml/model.py`, `ml/train.py`, `ml/publish.py`.
- **Layering:** logic in `web/app.py` routes; Rook prompt text in Python instead of
  `coach/prompts/`; CLI and MCP computing the same thing two different ways instead of going
  through `Analyzer`.
- **Async:** blocking SQLite or polars work on the event loop in web or MCP handlers.
- **Front end:** layouts that break at phone width; UI copy that's vague, jargon-heavy or
  inconsistent with the rest of the app.

## Low priority

- Naming, docstrings, simplifications. Mention only if it clearly helps.
- Docs (`README.md`, `src/crai/ml/README.md`) out of date with the change.
