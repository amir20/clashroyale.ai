"""Build a legacy (v2-layout) database for upgrade tests: the exact schema shipped before v3."""

import sqlite3
from contextlib import closing
from pathlib import Path

LEGACY_V2_SCHEMA = """
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

-- Seed lookups run before every analytics question; keep them off a full scan.
CREATE INDEX IF NOT EXISTS players_seed ON players(tag) WHERE is_seed = 1;

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
);

CREATE TABLE IF NOT EXISTS player_current_deck (
    tag             TEXT NOT NULL,
    slot            INTEGER NOT NULL,
    card_id         INTEGER NOT NULL,
    level           INTEGER,
    max_level       INTEGER,
    evolution_level INTEGER,
    PRIMARY KEY (tag, slot)
);

CREATE TABLE IF NOT EXISTS battles (
    id                   TEXT PRIMARY KEY,  -- sha1(battle_time + sorted participant tags)
    battle_time          TEXT NOT NULL,     -- ISO-8601 UTC
    participants_key     TEXT,              -- sorted participant tags joined by '|'
    battle_type          TEXT,              -- pathOfLegend, trail (ladder), riverRacePvP, ...
    game_mode_id         INTEGER,
    game_mode            TEXT,
    arena                TEXT,
    deck_selection       TEXT,              -- collection, draft, ...
    is_ladder_tournament INTEGER,
    is_hosted_match      INTEGER,
    league_number        INTEGER,
    team_size            INTEGER,           -- 1 = 1v1, 2 = 2v2
    ingested_at          TEXT
);
CREATE INDEX IF NOT EXISTS battles_time ON battles(battle_time);
CREATE INDEX IF NOT EXISTS battles_type ON battles(battle_type);

CREATE TABLE IF NOT EXISTS battle_participants (
    battle_id          TEXT NOT NULL,
    player_tag         TEXT NOT NULL,
    side               INTEGER NOT NULL,  -- 0/1; teammates share a side
    name               TEXT,
    clan_tag           TEXT,
    starting_trophies  INTEGER,
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
);
CREATE INDEX IF NOT EXISTS bp_tag ON battle_participants(player_tag);
CREATE INDEX IF NOT EXISTS bp_deck ON battle_participants(deck_key);

CREATE TABLE IF NOT EXISTS participant_cards (
    battle_id       TEXT NOT NULL,
    player_tag      TEXT NOT NULL,
    card_id         INTEGER NOT NULL,
    slot            INTEGER,           -- position in deck; evolution slots come first
    kind            TEXT NOT NULL,     -- 'deck' or 'support' (tower troop)
    level           INTEGER,
    max_level       INTEGER,
    evolution_level INTEGER,
    PRIMARY KEY (battle_id, player_tag, card_id)
);
CREATE INDEX IF NOT EXISTS pc_card ON participant_cards(card_id);

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


def make_legacy(v3_db: Path, dest: Path, *, version: int = 2) -> None:
    """Copy a v3 database into the old layout: SHA-1-style text battle ids, rowid tables."""
    with closing(sqlite3.connect(dest)) as conn:
        conn.executescript(LEGACY_V2_SCHEMA)
        conn.execute("ATTACH DATABASE ? AS src", (str(v3_db),))
        for t in ("cards", "players", "player_cards", "player_current_deck", "crawl_runs"):
            conn.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
        # Text ids in a deliberately non-sequential order, like real SHA-1s.
        conn.execute(
            """INSERT INTO main.battles (id, battle_time, participants_key, battle_type,
                   game_mode_id, game_mode, arena, deck_selection, is_ladder_tournament,
                   is_hosted_match, league_number, team_size, ingested_at)
               SELECT printf('%040x', id * 2654435761 % 4294967291), battle_time,
                      participants_key, battle_type, game_mode_id, game_mode, arena,
                      deck_selection, is_ladder_tournament, is_hosted_match, league_number,
                      team_size, ingested_at
               FROM src.battles ORDER BY id"""
        )
        for t in ("battle_participants", "participant_cards"):
            cols = [
                r[1] for r in conn.execute(f"PRAGMA src.table_info({t})") if r[1] != "battle_id"
            ]
            sel = ", ".join(f"x.{c}" for c in cols)
            conn.execute(
                f"""INSERT INTO main.{t} (battle_id, {", ".join(cols)})
                    SELECT printf('%040x', x.battle_id * 2654435761 % 4294967291), {sel}
                    FROM src.{t} x"""
            )
        if version < 2:
            conn.execute("UPDATE battles SET participants_key = NULL")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
