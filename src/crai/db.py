"""SQLite schema, migrations and idempotent ingest functions.

Every write is an upsert or INSERT OR IGNORE, so re-running a crawl only adds new data.
"""

import hashlib
import json
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import ApiBattle, ApiCard, ApiPlayer, BattlePlayer, CardList

# The same battle appears in both players' logs, sometimes with timestamps a second apart.
# Battles between the same participants within this window are treated as one.
DEDUP_WINDOW_SECONDS = 10

SCHEMA = """
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

SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        conn.execute("PRAGMA query_only = 1")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=60)  # wait for other writers
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        migrate(conn)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(path: Path | str, *, read_only: bool = False) -> Generator[sqlite3.Connection]:
    """Open, commit on success (writable only), and always close.

    Note `with sqlite3.connect(...)` alone only manages the transaction; it never closes.
    """
    conn = connect(path, read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        # v2: participants_key for timestamp-tolerant dedup; remove existing near-duplicates.
        if "participants_key" not in _columns(conn, "battles"):
            conn.execute("ALTER TABLE battles ADD COLUMN participants_key TEXT")
        conn.execute(
            """UPDATE battles SET participants_key = (
                 SELECT group_concat(player_tag, '|') FROM (
                   SELECT player_tag FROM battle_participants
                   WHERE battle_id = battles.id ORDER BY player_tag))
               WHERE participants_key IS NULL"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS battles_pkey ON battles(participants_key)")
        dedupe_battles(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS battles_pkey ON battles(participants_key)")


def dedupe_battles(conn: sqlite3.Connection) -> int:
    """Delete battles that duplicate an earlier one (same participants, within the window)."""
    dupes = [
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT b2.id FROM battles b1
               JOIN battles b2 ON b2.participants_key = b1.participants_key AND b2.id <> b1.id
               WHERE (b1.battle_time < b2.battle_time
                      OR (b1.battle_time = b2.battle_time AND b1.id < b2.id))
                 AND unixepoch(b2.battle_time) - unixepoch(b1.battle_time) <= ?""",
            (DEDUP_WINDOW_SECONDS,),
        )
    ]
    for table, col in (("participant_cards", "battle_id"), ("battle_participants", "battle_id")):
        conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(d,) for d in dupes])
    conn.executemany("DELETE FROM battles WHERE id = ?", [(d,) for d in dupes])
    return len(dupes)


def parse_battle_time(s: str) -> str:
    """'20261107T123456.000Z' -> '2026-11-07T12:34:56Z'."""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


# ---------------------------------------------------------------- cards


def upsert_cards(conn: sqlite3.Connection, payload: CardList) -> int:
    ts = now_iso()
    rows = [
        (
            c["id"],
            c.get("name") or str(c["id"]),
            c.get("rarity"),
            c.get("elixirCost"),
            c.get("maxLevel"),
            c.get("maxEvolutionLevel"),
            is_support,
            (c.get("iconUrls") or {}).get("medium"),
            (c.get("iconUrls") or {}).get("evolutionMedium"),
            ts,
        )
        for is_support, cards in (
            (0, (payload.get("items") or [])),
            (1, (payload.get("supportItems") or [])),
        )
        for c in cards
    ]
    conn.executemany(
        """INSERT INTO cards (id, name, rarity, elixir, max_level, max_evolution_level,
                              is_support, icon_url, evo_icon_url, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, rarity=excluded.rarity, elixir=excluded.elixir,
             max_level=excluded.max_level, max_evolution_level=excluded.max_evolution_level,
             is_support=excluded.is_support, icon_url=excluded.icon_url,
             evo_icon_url=excluded.evo_icon_url, updated_at=excluded.updated_at""",
        rows,
    )
    return len(rows)


def _ensure_cards(conn: sqlite3.Connection, cards: Iterable[ApiCard], is_support: int) -> None:
    """Make sure every card seen in a battle/profile exists in `cards` (even before /cards sync)."""
    conn.executemany(
        """INSERT OR IGNORE INTO cards (id, name, rarity, elixir, max_level, is_support, icon_url)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (
                c["id"],
                c.get("name") or str(c["id"]),
                c.get("rarity"),
                c.get("elixirCost"),
                c.get("maxLevel"),
                is_support,
                (c.get("iconUrls") or {}).get("medium"),
            )
            for c in cards
        ],
    )


# ---------------------------------------------------------------- players


def upsert_player(
    conn: sqlite3.Connection,
    p: ApiPlayer,
    *,
    is_seed: bool = False,
    seed_rank: int | None = None,
    depth: int | None = None,
) -> None:
    ts = now_iso()
    clan = p.get("clan") or {}
    pol = p.get("currentPathOfLegendSeasonResult") or {}
    best_pol = p.get("bestPathOfLegendSeasonResult") or {}
    tag = p["tag"]
    conn.execute(
        """INSERT INTO players (tag, name, exp_level, trophies, best_trophies, wins, losses,
               battle_count, three_crown_wins, clan_tag, clan_name, arena, pol_league, pol_trophies,
               pol_rank, best_pol_league, best_pol_rank, is_seed, seed_rank, crawl_depth,
               first_seen_at, profile_fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(tag) DO UPDATE SET
             name=excluded.name, exp_level=excluded.exp_level, trophies=excluded.trophies,
             best_trophies=excluded.best_trophies, wins=excluded.wins, losses=excluded.losses,
             battle_count=excluded.battle_count, three_crown_wins=excluded.three_crown_wins,
             clan_tag=excluded.clan_tag, clan_name=excluded.clan_name, arena=excluded.arena,
             pol_league=excluded.pol_league, pol_trophies=excluded.pol_trophies,
             pol_rank=excluded.pol_rank, best_pol_league=excluded.best_pol_league,
             best_pol_rank=excluded.best_pol_rank,
             is_seed=max(players.is_seed, excluded.is_seed),
             seed_rank=coalesce(excluded.seed_rank, players.seed_rank),
             crawl_depth=min(coalesce(players.crawl_depth, 99), coalesce(excluded.crawl_depth, 99)),
             profile_fetched_at=excluded.profile_fetched_at""",
        (
            tag,
            p.get("name"),
            p.get("expLevel"),
            p.get("trophies"),
            p.get("bestTrophies"),
            p.get("wins"),
            p.get("losses"),
            p.get("battleCount"),
            p.get("threeCrownWins"),
            clan.get("tag"),
            clan.get("name"),
            (p.get("arena") or {}).get("name"),
            pol.get("leagueNumber"),
            pol.get("trophies"),
            pol.get("rank"),
            best_pol.get("leagueNumber"),
            best_pol.get("rank"),
            int(is_seed),
            seed_rank,
            depth,
            ts,
            ts,
        ),
    )
    cards = p.get("cards") or []
    support = p.get("supportCards") or []
    _ensure_cards(conn, cards, 0)
    _ensure_cards(conn, support, 1)
    if cards or support:
        conn.execute("DELETE FROM player_cards WHERE tag = ?", (tag,))
        conn.executemany(
            """INSERT INTO player_cards (tag, card_id, level, max_level, star_level,
                                         evolution_level, count) VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    tag,
                    c["id"],
                    c.get("level"),
                    c.get("maxLevel"),
                    c.get("starLevel"),
                    c.get("evolutionLevel"),
                    c.get("count"),
                )
                for c in [*cards, *support]
            ],
        )
    if deck := (p.get("currentDeck") or []):
        conn.execute("DELETE FROM player_current_deck WHERE tag = ?", (tag,))
        conn.executemany(
            """INSERT INTO player_current_deck
                 (tag, slot, card_id, level, max_level, evolution_level) VALUES (?,?,?,?,?,?)""",
            [
                (tag, i, c["id"], c.get("level"), c.get("maxLevel"), c.get("evolutionLevel"))
                for i, c in enumerate(deck)
            ],
        )


def mark_battlelog_fetched(conn: sqlite3.Connection, tag: str) -> None:
    ts = now_iso()
    conn.execute(
        """INSERT INTO players (tag, first_seen_at, battlelog_fetched_at) VALUES (?, ?, ?)
           ON CONFLICT(tag) DO UPDATE SET battlelog_fetched_at = excluded.battlelog_fetched_at""",
        (tag, ts, ts),
    )


# ---------------------------------------------------------------- battles


def participants(b: ApiBattle) -> list[BattlePlayer]:
    return [*(b.get("team") or []), *(b.get("opponent") or [])]


def _tags(b: ApiBattle) -> list[str]:
    return sorted(p.get("tag", "?") for p in participants(b))


def battle_id(b: ApiBattle) -> str:
    return hashlib.sha1(f"{b.get('battleTime')}|{'|'.join(_tags(b))}".encode()).hexdigest()


def _result(mine: int, theirs: int) -> str:
    return "win" if mine > theirs else "loss" if mine < theirs else "draw"


def _is_duplicate(conn: sqlite3.Connection, participants_key: str, battle_time: str) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM battles WHERE participants_key = ?
               AND abs(unixepoch(battle_time) - unixepoch(?)) <= ? LIMIT 1""",
            (participants_key, battle_time, DEDUP_WINDOW_SECONDS),
        ).fetchone()
        is not None
    )


def ingest_battle(conn: sqlite3.Connection, b: ApiBattle) -> bool:
    """Insert one battle (from either participant's battlelog). Returns True if it was new."""
    team, opp = (b.get("team") or []), (b.get("opponent") or [])
    if not team or not opp:
        return False
    bid = battle_id(b)
    pkey = "|".join(_tags(b))
    btime = parse_battle_time(b.get("battleTime", ""))
    if _is_duplicate(conn, pkey, btime):
        return False
    # Canonical side: side 0 holds the lexicographically smallest tag, so the same battle seen
    # from either player's log is stored identically.
    sides: list[list[BattlePlayer]] = [team, opp]
    if min(p.get("tag", "") for p in opp) < min(p.get("tag", "") for p in team):
        sides = [opp, team]
    game_mode = b.get("gameMode") or {}
    cur = conn.execute(
        """INSERT OR IGNORE INTO battles (id, battle_time, participants_key, battle_type,
               game_mode_id, game_mode, arena, deck_selection, is_ladder_tournament,
               is_hosted_match, league_number, team_size, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bid,
            btime,
            pkey,
            b.get("type"),
            game_mode.get("id"),
            game_mode.get("name"),
            (b.get("arena") or {}).get("name"),
            b.get("deckSelection"),
            int(b.get("isLadderTournament") or False),
            int(b.get("isHostedMatch") or False),
            b.get("leagueNumber"),
            len(team),
            now_iso(),
        ),
    )
    if cur.rowcount == 0:
        return False

    side_crowns = [max((p.get("crowns") or 0) for p in s) for s in sides]
    for side_idx, side in enumerate(sides):
        mine, theirs = side_crowns[side_idx], side_crowns[1 - side_idx]
        for p in side:
            if tag := p.get("tag"):
                _ingest_participant(conn, bid, side_idx, p, tag, mine, theirs)
    return True


def _ingest_participant(
    conn: sqlite3.Connection,
    bid: str,
    side_idx: int,
    p: BattlePlayer,
    tag: str,
    mine: int,
    theirs: int,
) -> None:
    cards = p.get("cards") or []
    support = p.get("supportCards") or []
    _ensure_cards(conn, cards, 0)
    _ensure_cards(conn, support, 1)
    deck_key = "-".join(str(i) for i in sorted(c["id"] for c in cards)) if len(cards) == 8 else None
    elixirs = [e for c in cards if (e := c.get("elixirCost")) is not None]
    conn.execute(
        "INSERT INTO players (tag, name, first_seen_at) VALUES (?,?,?) ON CONFLICT(tag) DO NOTHING",
        (tag, p.get("name"), now_iso()),
    )
    conn.execute(
        """INSERT OR IGNORE INTO battle_participants (battle_id, player_tag, side, name,
               clan_tag, starting_trophies, trophy_change, crowns, opponent_crowns, result,
               king_tower_hp, princess_towers_hp, elixir_leaked, deck_key, avg_elixir)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bid,
            tag,
            side_idx,
            p.get("name"),
            (p.get("clan") or {}).get("tag"),
            p.get("startingTrophies"),
            p.get("trophyChange"),
            p.get("crowns"),
            theirs,
            _result(mine, theirs),
            p.get("kingTowerHitPoints"),
            json.dumps(p.get("princessTowersHitPoints")),
            p.get("elixirLeaked"),
            deck_key,
            round(sum(elixirs) / len(elixirs), 2) if elixirs else None,
        ),
    )
    conn.executemany(
        """INSERT OR IGNORE INTO participant_cards (battle_id, player_tag, card_id, slot, kind,
               level, max_level, evolution_level) VALUES (?,?,?,?,?,?,?,?)""",
        [
            (bid, tag, c["id"], i, kind, c.get("level"), c.get("maxLevel"), c.get("evolutionLevel"))
            for kind, lst in (("deck", cards), ("support", support))
            for i, c in enumerate(lst)
        ],
    )


def ingest_battlelog(conn: sqlite3.Connection, battles: Sequence[ApiBattle]) -> int:
    return sum(ingest_battle(conn, b) for b in battles)


def opponents_of(conn: sqlite3.Connection, tag: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT o.player_tag FROM battle_participants me
           JOIN battle_participants o
             ON o.battle_id = me.battle_id AND o.player_tag <> me.player_tag
           WHERE me.player_tag = ?""",
        (tag,),
    ).fetchall()
    return [r[0] for r in rows]
