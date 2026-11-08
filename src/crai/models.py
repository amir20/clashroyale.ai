"""Typed shapes of the Clash Royale API payloads we consume.

Only the fields we read are declared. Everything is optional (`total=False`) because the API
omits fields freely (e.g. no `startingTrophies` in Path of Legend battles).
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
    iconUrls: NotRequired[IconUrls]


class CardList(TypedDict, total=False):
    items: list[ApiCard]
    supportItems: list[ApiCard]


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
    clan: NotRequired[Named]
    arena: NotRequired[Named]
    currentPathOfLegendSeasonResult: NotRequired[PathOfLegendResult]
    bestPathOfLegendSeasonResult: NotRequired[PathOfLegendResult]
    cards: NotRequired[list[ApiCard]]
    supportCards: NotRequired[list[ApiCard]]
    currentDeck: NotRequired[list[ApiCard]]


class BattlePlayer(TypedDict, total=False):
    tag: str
    name: str
    clan: Named
    startingTrophies: int
    trophyChange: int
    crowns: int
    kingTowerHitPoints: int
    princessTowersHitPoints: list[int] | None
    elixirLeaked: float
    cards: list[ApiCard]
    supportCards: list[ApiCard]


class ApiBattle(TypedDict, total=False):
    type: str
    battleTime: str
    isLadderTournament: bool
    isHostedMatch: bool
    leagueNumber: int
    deckSelection: str
    arena: Named
    gameMode: Named
    team: list[BattlePlayer]
    opponent: list[BattlePlayer]


class RankedPlayer(TypedDict, total=False):
    tag: str
    name: str
    rank: int
    eloRating: int


class Season(TypedDict, total=False):
    id: str
    code: str


class ItemsPage[T](TypedDict, total=False):
    items: list[T]
