"""SQLite schema (v3) and legacy-format detection.

v3 layout is chosen for write speed on a multi-GB database: battle ids are sequential integers
and the per-battle child tables are `WITHOUT ROWID`, clustered on `(battle_id, ...)`. New rows
therefore land at the *end* of every large B-tree, so a commit touches a few adjacent pages
instead of thousands of random ones (v2 keyed everything on a random SHA-1, which made commits
~80% of crawl time). Secondary indexes on tags/keys still take one random write each; the
deck_key index was dropped because analytics reads the Parquet snapshot instead.
"""

import sqlite3

SCHEMA_VERSION = 3

TABLES = """
CREATE TABLE IF NOT EXISTS cards (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    rarity              TEXT,
    elixir              INTEGER,
    max_level           INTEGER,
    max_evolution_level INTEGER,
    is_support          INTEGER NOT NULL DEFAULT 0,  -- tower troops
    icon_url            TEXT,
    evo_icon_url        TEXT,
    hero_icon_url       TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS players (
    tag                  TEXT PRIMARY KEY,
    name                 TEXT,
    exp_level            INTEGER,
    trophies             INTEGER,
    best_trophies        INTEGER,
    wins                 INTEGER,
    losses               INTEGER,
    battle_count         INTEGER,
    three_crown_wins     INTEGER,
    clan_tag             TEXT,
    clan_name            TEXT,
    arena                TEXT,
    pol_league           INTEGER,   -- current Path of Legend season
    pol_trophies         INTEGER,
    pol_rank             INTEGER,
    best_pol_league      INTEGER,
    best_pol_rank        INTEGER,
    is_seed              INTEGER NOT NULL DEFAULT 0,  -- 1 = top-ranked or explicitly requested
    seed_rank            INTEGER,
    crawl_depth          INTEGER,
    first_seen_at        TEXT,
    profile_fetched_at   TEXT,
    battlelog_fetched_at TEXT
);

-- Card collection (levels) as reported by the player profile.
CREATE TABLE IF NOT EXISTS player_cards (
    tag             TEXT NOT NULL,
    card_id         INTEGER NOT NULL,
    level           INTEGER,
    max_level       INTEGER,
    star_level      INTEGER,
    evolution_level INTEGER,
    count           INTEGER,
    PRIMARY KEY (tag, card_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS player_current_deck (
    tag             TEXT NOT NULL,
    slot            INTEGER NOT NULL,
    card_id         INTEGER NOT NULL,
    level           INTEGER,
    max_level       INTEGER,
    evolution_level INTEGER,
    PRIMARY KEY (tag, slot)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS battles (
    id                   INTEGER PRIMARY KEY,  -- sequential: new battles append
    battle_time          TEXT NOT NULL,        -- ISO-8601 UTC
    participants_key     TEXT NOT NULL,        -- sorted participant tags joined by '|' (dedup)
    battle_type          TEXT,                 -- pathOfLegend, trail (ladder), riverRacePvP, ...
    game_mode_id         INTEGER,
    game_mode            TEXT,
    arena                TEXT,
    deck_selection       TEXT,                 -- collection, draft, ...
    is_ladder_tournament INTEGER,
    is_hosted_match      INTEGER,
    league_number        INTEGER,
    team_size            INTEGER,              -- 1 = 1v1, 2 = 2v2
    ingested_at          TEXT
);

CREATE TABLE IF NOT EXISTS battle_participants (
    battle_id          INTEGER NOT NULL,  -- battles.id
    player_tag         TEXT NOT NULL,
    side               INTEGER NOT NULL,  -- 0/1; teammates share a side
    name               TEXT,
    clan_tag           TEXT,
    starting_trophies  INTEGER,           -- ladder trophies, or Path of Legend rating
    trophy_change      INTEGER,
    crowns             INTEGER,
    opponent_crowns    INTEGER,
    result             TEXT,              -- win / loss / draw
    king_tower_hp      INTEGER,
    princess_towers_hp TEXT,              -- JSON list
    elixir_leaked      REAL,
    deck_key           TEXT,              -- sorted card ids joined by '-' (8-card decks only)
    avg_elixir         REAL,
    PRIMARY KEY (battle_id, player_tag)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS participant_cards (
    battle_id       INTEGER NOT NULL,  -- battles.id
    player_tag      TEXT NOT NULL,
    card_id         INTEGER NOT NULL,
    slot            INTEGER,           -- position in deck; evolution slots come first
    kind            TEXT NOT NULL,     -- 'deck' or 'support' (tower troop)
    level           INTEGER,
    max_level       INTEGER,
    evolution_level INTEGER,
    PRIMARY KEY (battle_id, player_tag, card_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    params          TEXT,
    players_fetched INTEGER,
    battles_new     INTEGER,
    requests        INTEGER,
    status          TEXT
);
"""

# Secondary indexes. Kept separate so a bulk load can build them after inserting the rows.
INDEXES = """
CREATE INDEX IF NOT EXISTS players_seed ON players(tag) WHERE is_seed = 1;
CREATE INDEX IF NOT EXISTS battles_pkey ON battles(participants_key);
CREATE INDEX IF NOT EXISTS battles_time ON battles(battle_time);
CREATE INDEX IF NOT EXISTS battles_type ON battles(battle_type);
CREATE INDEX IF NOT EXISTS bp_tag ON battle_participants(player_tag);
CREATE INDEX IF NOT EXISTS pc_card ON participant_cards(card_id);
"""

SCHEMA = TABLES + INDEXES


class LegacySchemaError(RuntimeError):
    def __init__(self, path: object) -> None:
        super().__init__(
            f"{path} uses the old (v2) layout, which makes crawls slow. "
            "Run `uv run crai upgrade-db` once to convert it (your data is kept)."
        )


def is_legacy(conn: sqlite3.Connection) -> bool:
    """True for a pre-v3 database (battles keyed by a SHA-1 text id)."""
    row = conn.execute("SELECT type FROM pragma_table_info('battles') WHERE name = 'id'").fetchone()
    return row is not None and row[0].upper() == "TEXT"
