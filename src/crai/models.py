"""Typed shapes of the Clash Royale API payloads we consume.

Only the fields we read are declared. Fields are optional because the API omits them freely
(e.g. no `startingTrophies` in Path of Legend battles), and nested objects/lists are `| None`
because it also sends explicit nulls (e.g. `currentPathOfLegendSeasonResult: null`). Always read
them as `x.get(k) or {}`, never `x.get(k, {})`, which returns None for an explicit null.
"""

from typing import NotRequired, TypedDict


class IconUrls(TypedDict, total=False):
    medium: str
    evolutionMedium: str


class ApiCard(TypedDict):
    id: int
    name: NotRequired[str]
    rarity: NotRequired[str]
    elixirCost: NotRequired[int]
    maxLevel: NotRequired[int]
    maxEvolutionLevel: NotRequired[int]
    level: NotRequired[int]
    starLevel: NotRequired[int]
    evolutionLevel: NotRequired[int]
    count: NotRequired[int]
    iconUrls: NotRequired[IconUrls | None]


class CardList(TypedDict, total=False):
    items: list[ApiCard] | None
    supportItems: list[ApiCard] | None


class Named(TypedDict, total=False):
    id: int
    tag: str
    name: str


class PathOfLegendResult(TypedDict, total=False):
    leagueNumber: int
    trophies: int
    rank: int | None


class ApiPlayer(TypedDict):
    tag: str
    name: NotRequired[str]
    expLevel: NotRequired[int]
    trophies: NotRequired[int]
    bestTrophies: NotRequired[int]
    wins: NotRequired[int]
    losses: NotRequired[int]
    battleCount: NotRequired[int]
    threeCrownWins: NotRequired[int]
    clan: NotRequired[Named | None]
    arena: NotRequired[Named | None]
    currentPathOfLegendSeasonResult: NotRequired[PathOfLegendResult | None]
    bestPathOfLegendSeasonResult: NotRequired[PathOfLegendResult | None]
    cards: NotRequired[list[ApiCard] | None]
    supportCards: NotRequired[list[ApiCard] | None]
    currentDeck: NotRequired[list[ApiCard] | None]


class BattlePlayer(TypedDict, total=False):
    tag: str
    name: str
    clan: Named | None
    startingTrophies: int
    trophyChange: int
    crowns: int | None
    kingTowerHitPoints: int
    princessTowersHitPoints: list[int] | None
    elixirLeaked: float
    cards: list[ApiCard] | None
    supportCards: list[ApiCard] | None


class ApiBattle(TypedDict, total=False):
    type: str
    battleTime: str
    isLadderTournament: bool
    isHostedMatch: bool
    leagueNumber: int
    deckSelection: str
    arena: Named | None
    gameMode: Named | None
    team: list[BattlePlayer] | None
    opponent: list[BattlePlayer] | None


class RankedPlayer(TypedDict, total=False):
    tag: str
    name: str
    rank: int
    eloRating: int


class Season(TypedDict, total=False):
    id: str
    code: str


class ItemsPage[T](TypedDict, total=False):
    items: list[T] | None
