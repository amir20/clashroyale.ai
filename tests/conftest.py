"""Synthetic API payloads and a pre-populated database."""

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crai.analysis.analytics import Analyzer
from crai.collect.models import ApiBattle, ApiCard, ApiPlayer, BattlePlayer, CardList
from crai.storage import database as db

CARDS = frozendict(
    {
        26000000: ("Knight", 3, "Common"),
        26000021: ("Hog Rider", 4, "Rare"),
        26000004: ("P.E.K.K.A", 7, "Epic"),
        27000008: ("X-Bow", 6, "Epic"),
        26000055: ("Mega Knight", 7, "Legendary"),
        28000011: ("The Log", 2, "Legendary"),
        28000000: ("Fireball", 4, "Rare"),
        26000010: ("Skeletons", 1, "Common"),
        26000030: ("Ice Spirit", 1, "Common"),
        26000014: ("Musketeer", 4, "Rare"),
        27000000: ("Cannon", 3, "Common"),
        26000012: ("Skeleton Army", 3, "Epic"),
    }
)
MAX_LEVEL = frozendict(Common=16, Rare=14, Epic=11, Legendary=8)
HOG = 26000021
PEKKA = 26000004
CYCLE = (HOG, 28000011, 28000000, 26000010, 26000030, 26000014, 27000000, 26000000)
BEATDOWN = (PEKKA, 26000055, 28000011, 28000000, 26000010, 26000012, 26000014, 26000000)
# Recent, so the default 7-day window includes the fixture battles whatever today's date is.
START = (datetime.now(UTC) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
ME = "#2P"


def card(cid: int) -> ApiCard:
    name, elixir, rarity = CARDS[cid]
    return {
        "id": cid,
        "name": name,
        "elixirCost": elixir,
        "rarity": rarity,
        "level": MAX_LEVEL[rarity] - 1,
        "maxLevel": MAX_LEVEL[rarity],
        "evolutionLevel": 0,
        "iconUrls": {"medium": "x"},
    }


def card_list() -> CardList:
    return {"items": [card(cid) for cid in CARDS]}


def player(tag: str, deck: tuple[int, ...], crowns: int) -> BattlePlayer:
    return {
        "tag": tag,
        "name": tag[1:],
        "crowns": crowns,
        "startingTrophies": 9000,
        "trophyChange": 30,
        "cards": [card(c) for c in deck],
        "supportCards": [],
        "kingTowerHitPoints": 1000,
    }


def battle(when: datetime, a: str, b: str, *, a_wins: bool) -> ApiBattle:
    return {
        "type": "pathOfLegend",
        "battleTime": when.strftime("%Y%m%dT%H%M%S.000Z"),
        "deckSelection": "collection",
        "gameMode": {"id": 72000464, "name": "Ranked1v1"},
        "arena": {"name": "Legendary Arena"},
        "team": [player(a, CYCLE, 3 if a_wins else 0)],
        "opponent": [player(b, BEATDOWN, 0 if a_wins else 1)],
    }


def flipped(b: ApiBattle, *, shift_seconds: int = 0) -> ApiBattle:
    """The same battle as it appears in the opponent's log."""
    t = datetime.strptime(b["battleTime"], "%Y%m%dT%H%M%S.%fZ") + timedelta(seconds=shift_seconds)
    return b | {
        "team": b["opponent"],
        "opponent": b["team"],
        "battleTime": t.strftime("%Y%m%dT%H%M%S.000Z"),
    }


def battles(n: int = 200, seed: int = 1) -> list[ApiBattle]:
    rng = random.Random(seed)
    return [
        battle(
            START + timedelta(minutes=5 * i), ME, f"#{'2' * (i % 5 + 2)}", a_wins=rng.random() < 0.7
        )
        for i in range(n)
    ]


def profile(tag: str = ME) -> ApiPlayer:
    return {
        "tag": tag,
        "name": "me",
        "cards": [card(c) for c in (*CYCLE, PEKKA)],
        "currentDeck": [card(c) for c in CYCLE],
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_cards(conn, card_list())
        assert db.ingest_battlelog(conn, battles()) == 200
        db.upsert_player(conn, profile(), is_seed=True)
    return path


@pytest.fixture
def analyzer(db_path: Path) -> Analyzer:
    return Analyzer(db_path)
