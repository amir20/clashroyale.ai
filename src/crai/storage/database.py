"""SQLite connections and idempotent ingest functions (schema lives in `schema.py`).

Every write is an upsert, INSERT OR IGNORE, or guarded by the dedup check, so re-running a crawl
only adds new data.
"""

import json
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from crai.collect.models import ApiBattle, ApiCard, ApiPlayer, BattlePlayer, CardList
from crai.storage.schema import SCHEMA, SCHEMA_VERSION, LegacySchemaError, is_legacy

# The same battle appears in both players' logs, sometimes with timestamps a second apart.
# Battles between the same participants within this window are treated as one.
DEDUP_WINDOW_SECONDS = 10


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        conn.execute("PRAGMA query_only = 1")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the crawler hands this connection to one writer thread; every
        # use is serialized (writer thread, or the loop while the writer is idle).
        conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        # SQLite never shrinks the WAL file by default; cap what it keeps after a checkpoint.
        conn.execute("PRAGMA journal_size_limit = 67108864")  # 64 MB
    # Bigger page cache + memory-mapped reads: fewer disk round trips on a multi-GB file.
    conn.execute("PRAGMA cache_size = -262144")  # 256 MB
    conn.execute("PRAGMA mmap_size = 1073741824")  # 1 GB
    conn.execute("PRAGMA temp_store = MEMORY")
    if is_legacy(conn):
        conn.close()
        raise LegacySchemaError(path)
    if not read_only:
        conn.executescript(SCHEMA)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
        if "hero_icon_url" not in columns:  # added after v3 shipped
            conn.execute("ALTER TABLE cards ADD COLUMN hero_icon_url TEXT")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.row_factory = sqlite3.Row
    return conn


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL back into the database and truncate it (e.g. at the end of a crawl)."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
            (c.get("iconUrls") or {}).get("heroMedium"),
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
                              is_support, icon_url, evo_icon_url, hero_icon_url, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, rarity=excluded.rarity, elixir=excluded.elixir,
             max_level=excluded.max_level, max_evolution_level=excluded.max_evolution_level,
             is_support=excluded.is_support, icon_url=excluded.icon_url,
             evo_icon_url=excluded.evo_icon_url, hero_icon_url=excluded.hero_icon_url,
             updated_at=excluded.updated_at""",
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
        """INSERT INTO battles (battle_time, participants_key, battle_type,
               game_mode_id, game_mode, arena, deck_selection, is_ladder_tournament,
               is_hosted_match, league_number, team_size, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
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
    bid = cur.lastrowid
    assert bid is not None
    side_crowns = [max((p.get("crowns") or 0) for p in s) for s in sides]
    for side_idx, side in enumerate(sides):
        mine, theirs = side_crowns[side_idx], side_crowns[1 - side_idx]
        for p in side:
            if tag := p.get("tag"):
                _ingest_participant(conn, bid, side_idx, p, tag, mine, theirs)
    return True


def _ingest_participant(
    conn: sqlite3.Connection,
    bid: int,
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


def prune_battles(conn: sqlite3.Connection, days: int) -> int:
    """Delete battles older than `days`, keeping every battle a seed player was in.

    Seeds are the top players and you, so your own history is never removed. Freed pages are
    reused by later crawls, so the database file stops growing instead of shrinking.
    """
    cutoff = f"-{days} days"
    old = conn.execute(
        """SELECT b.id FROM battles b
           WHERE b.battle_time < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
             AND NOT EXISTS (
               SELECT 1 FROM battle_participants bp JOIN players p ON p.tag = bp.player_tag
               WHERE bp.battle_id = b.id AND p.is_seed = 1)""",
        (cutoff,),
    ).fetchall()
    ids = [(r[0],) for r in old]
    conn.executemany("DELETE FROM participant_cards WHERE battle_id = ?", ids)
    conn.executemany("DELETE FROM battle_participants WHERE battle_id = ?", ids)
    conn.executemany("DELETE FROM battles WHERE id = ?", ids)
    return len(ids)
