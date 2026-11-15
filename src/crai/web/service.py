"""Shared state behind the routes: building reports, answering questions, and usage limits."""

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from crai.analysis import insights, visuals
from crai.analysis.analytics import DEFAULT_SINCE_DAYS, Analyzer
from crai.coach import voice
from crai.coach.chats import ChatLog
from crai.collect.client import ClashClient
from crai.config import Settings
from crai.ml import explain
from crai.storage import database as db
from crai.timing import Laps

log = logging.getLogger(__name__)

FRESH_SECONDS = 600
DAILY_QUESTIONS = 5
HEARTBEAT_SECONDS = 1.6
LOADS_PER_MINUTE = 20
# A report picks up new battles itself only when there are this few (someone's own battlelog).
# More means the crawler just wrote a batch: loading it takes the NAS a while, so the background
# refresher does it while reports use the table they have. The server also converts at most this
# many battles from SQLite; the crawler writes the rest to the snapshot.
INLINE_BATTLES = 2_000
REFRESH_SECONDS = 300  # how often the background refresher catches up with the crawler
REFRESH_POLL_SECONDS = 5
QUESTIONS = {
    "why_losing": "Why do I keep losing?",
    "replace_card": "Which card should I replace?",
    "best_cards": "Which of my cards do I play better than other people?",
    "nemesis": "Which cards beat me the most?",
    "best_deck": "What's my best deck?",
}

type QuestionId = Literal["why_losing", "replace_card", "best_cards", "nemesis", "best_deck"]


class Usage:
    """Per-day counters for custom AI questions, in their own small database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS usage
                   (day TEXT, who TEXT, n INTEGER, PRIMARY KEY (day, who))"""
            )
            c.commit()

    def take(self, who: str, limit: int) -> int | None:
        """Consume one question. Returns how many are left today, or None if none remain."""
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            row = c.execute("SELECT n FROM usage WHERE day = ? AND who = ?", (day, who)).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                return None
            c.execute(
                """INSERT INTO usage VALUES (?, ?, 1)
                   ON CONFLICT(day, who) DO UPDATE SET n = n + 1""",
                (day, who),
            )
            c.commit()
        return limit - used - 1


class RateLimiter:
    """Sliding-window limit per client, so one visitor can't burn the API keys."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self.hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, who: str) -> bool:
        now, hits = time.monotonic(), self.hits[who]
        while hits and now - hits[0] > self.seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class Service:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.settings = settings
        self.transport = transport
        # The crawler writes its battles to the Parquet snapshot itself; the server converts only
        # the few it stores for visitors.
        # Lean: only the battles reports use, held once. A week, the default every stat uses,
        # unless CRAI_ANALYTICS_DAYS says otherwise.
        self.analyzer = Analyzer(
            settings.db_path,
            settings.analytics_days or DEFAULT_SINCE_DAYS,
            follow=False,
            sync_limit=INLINE_BATTLES,
            lean=True,
        )
        self.lock = asyncio.Lock()  # one writer and one analytics load at a time
        self.usage = Usage(settings.db_path.with_name("app.db"))
        self.chats = ChatLog(settings.db_path.with_name("app.db"))
        self.fetched_at: dict[str, float] = {}
        self.loads = RateLimiter(LOADS_PER_MINUTE, 60)
        self.warming: asyncio.Future[None] | None = None  # the startup warm-up, while it runs
        self.wants_refresh = False  # a report saw the crawler ahead; refresh soon

    def client(self) -> ClashClient:
        return ClashClient(
            self.settings.api_tokens, self.settings.api_base, transport=self.transport
        )

    def warm_up(self) -> None:
        """Load the deck table and the stats every report shares, before anyone asks."""
        self.analyzer.decks()
        insights.elixir_table(self.analyzer)
        insights.playable_cards(self.analyzer)

    def known(self, tag: str) -> bool:
        if not self.settings.db_path.exists():
            return False
        with db.session(self.settings.db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT profile_fetched_at FROM players WHERE tag = ?", (tag,)
            ).fetchone()
        return bool(row and row[0])

    def player_card(
        self, tag: str, profile: Any = None, battles: list[Any] | None = None
    ) -> dict[str, Any] | None:
        """Who's being loaded, for the loading screen: from the profile and battles just fetched,
        or the ones stored. The deck is the one from their newest battle: the profile's
        `currentDeck` is whichever deck is selected in the game, maybe for another mode."""
        if profile is not None:
            clan = profile.get("clan") or {}
            arena = profile.get("arena") or {}
            pol = profile.get("currentPathOfLegendSeasonResult") or {}
            newest = next(
                (
                    b
                    for b in battles or []
                    if len(b.get("team") or []) == 1
                    and (b.get("deckSelection") or "collection") == "collection"
                ),
                None,
            )
            cards = (newest["team"][0].get("cards") if newest else None) or profile.get(
                "currentDeck"
            )
            deck = []
            for c in cards or []:
                icons, level = c.get("iconUrls") or {}, c.get("evolutionLevel") or 0
                icon = (icons.get("heroMedium") if level & 2 else None) or (
                    icons.get("evolutionMedium") if level & 1 else None
                )
                deck.append({"name": c.get("name"), "icon": icon or icons.get("medium")})
            return {
                "name": profile.get("name"),
                "level": profile.get("expLevel"),
                "trophies": profile.get("trophies"),
                "best_trophies": profile.get("bestTrophies"),
                "wins": profile.get("wins"),
                "battles": profile.get("battleCount"),
                "three_crowns": profile.get("threeCrownWins"),
                "clan": clan.get("name"),
                "arena": arena.get("name"),
                "mode": newest.get("type") if newest else None,
                "league": pol.get("leagueNumber"),
                "rating": pol.get("trophies"),
                "rank": pol.get("rank"),
                "deck": deck,
            }
        if not self.settings.db_path.exists():
            return None
        with db.session(self.settings.db_path, read_only=True) as conn:
            r = conn.execute(
                """SELECT name, exp_level, trophies, best_trophies, wins, battle_count,
                          three_crown_wins, clan_name, arena, pol_league, pol_trophies, pol_rank
                   FROM players WHERE tag = ?""",
                (tag,),
            ).fetchone()
            newest = conn.execute(
                """SELECT b.id, b.battle_type FROM battle_participants bp
                   JOIN battles b ON b.id = bp.battle_id
                   WHERE bp.player_tag = ? AND b.team_size = 1
                     AND coalesce(b.deck_selection, 'collection') = 'collection'
                   ORDER BY b.battle_time DESC LIMIT 1""",
                (tag,),
            ).fetchone()
            deck = []
            if newest:
                for name, level, icon, evo, hero in conn.execute(
                    """SELECT c.name, coalesce(pc.evolution_level, 0), c.icon_url,
                              c.evo_icon_url, c.hero_icon_url
                       FROM participant_cards pc JOIN cards c ON c.id = pc.card_id
                       WHERE pc.battle_id = ? AND pc.player_tag = ? AND pc.kind = 'deck'
                       ORDER BY pc.slot""",
                    (newest[0], tag),
                ):
                    form = (hero if level & 2 else None) or (evo if level & 1 else None)
                    deck.append({"name": name, "icon": form or icon})
        if r is None or r[0] is None:
            return None
        keys = ("name", "level", "trophies", "best_trophies", "wins", "battles", "three_crowns")
        keys += ("clan", "arena", "league", "rating", "rank")
        return dict(zip(keys, r, strict=True)) | {
            "mode": newest[1] if newest else None,
            "deck": deck,
        }

    def store(self, profile: Any, battles: list[Any], tag: str) -> int:
        laps = Laps()
        with laps.lap("sqlite"), db.session(self.settings.db_path) as conn:
            db.upsert_player(conn, profile)
            new = db.ingest_battlelog(conn, battles)
            db.mark_battlelog_fetched(conn, tag)
            db.track_player(conn, tag)
        # The snapshot catches up in `fresh` or the background refresher, not here: the crawler
        # may have written millions of battles since, and this visitor shouldn't wait on them.
        log.info("stored %s: %d new battles in %.2fs (%s)", tag, new, laps.total, laps)
        return new

    def fresh(self) -> None:
        """Pick up new battles now if there are only a few; otherwise leave it to the refresher."""
        a = self.analyzer
        if a._base_df is None:
            a.refresh()
        elif a.behind() > INLINE_BATTLES or not a.refresh(wait=False):
            # Too many to absorb now, or the refresher is already at it: use what's loaded.
            self.wants_refresh = True

    def refresh(self) -> None:
        """The background refresher's work: load what the crawler added to the snapshot."""
        self.wants_refresh = False
        self.analyzer.refresh()

    def report(self, tag: str, on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
        """The whole report. `on_step` hears each section's name as it starts."""
        a, laps = self.analyzer, Laps(on_step)
        with laps.lap("data"):
            self.fresh()  # its own lap, so the cost of new battles shows separately
        with laps.lap("games"):
            items = insights.games(a, tag)
        with laps.lap("overview"):
            overview = insights.overview(a, tag, items)
            patterns = insights.wins_and_losses(items)
            battles = insights.recent_battles(a, items)
        with laps.lap("similar"):
            similar = insights.similar_battles(a, tag, items) if items else None
        story = self.story(tag, items, laps)
        with laps.lap("model"):
            model = self.model(tag, items, story["replace"])
        story["lines"]["pick"] = (
            voice.model_pick(model["pick"], model["field"]["where"]) if model else None
        )
        with laps.lap("cards"):
            cards = insights.playable_cards(a)
        log.info("report %s in %.2fs (%s)", tag, laps.total, laps)
        return {
            "overview": overview,
            "intro": voice.intro(overview),
            "battles": battles,
            "similar": similar,
            "patterns": patterns,
            "patterns_line": voice.wins_and_losses(patterns),
            "story": story,
            "model": model,
            "cards": cards,
            "questions": [{"id": k, "label": v} for k, v in QUESTIONS.items()],
            "custom_questions_per_day": DAILY_QUESTIONS,
            "ai_enabled": bool(self.settings.openrouter_key),
            "dev": self.settings.dev,
        }

    @property
    def model_path(self) -> Path:
        return self.settings.db_path.parent / "ml" / "model.json"

    def model(
        self, tag: str, items: list[insights.Game], replace: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The deck model's view, adding its verdict to each suggested swap. A broken or missing
        model leaves the rest of the report alone."""
        try:
            return explain.report(self.analyzer, self.model_path, tag, items, replace)
        except Exception:
            log.exception("deck model failed for %s", tag)
            return None

    def story(self, tag: str, items: list[insights.Game], laps: Laps) -> dict[str, Any]:
        """The swipeable cards, computed with no AI call. A card with no data is None."""
        a = self.analyzer

        def usable(view: dict[str, Any]) -> dict[str, Any] | None:
            return None if "error" in view else view

        with laps.lap("trend"):
            trend = usable(visuals.trend_view(a, items))
        with laps.lap("close"):
            close = usable(visuals.close_games_view(items))
        with laps.lap("worst"):
            worst = usable(visuals.worst_matchups_view(a, items))
        with laps.lap("elixir"):
            elixir = usable(visuals.elixir_view(a, items))
        with laps.lap("replace"):
            replace = insights.replace_card(a, tag, items)
        with laps.lap("surprises"):
            found = insights.surprises(a, tag, items)
        return {
            "surprises": [x | voice.surprise(x) for x in found],
            "trend": trend,
            "close": close,
            "worst": worst,
            "elixir": elixir,
            "replace": replace,
            "lines": {
                "trend": voice.trend(trend) if trend else None,
                "close": voice.close_games(close) if close else None,
                "worst": voice.worst_matchups(worst) if worst else None,
                "elixir": voice.elixir(elixir) if elixir else None,
                "replace": voice.try_line(replace),
            },
        }

    def answer(self, tag: str, question: QuestionId) -> dict[str, Any]:
        a, items = self.analyzer, insights.games(self.analyzer, tag)
        if question == "why_losing":
            data, line, tool = (
                insights.game_plan(a, tag, items),
                voice.game_plan,
                "how_to_win_more",
            )
        elif question == "replace_card":
            data, line, tool = (
                insights.replace_card(a, tag, items),
                voice.replace_card,
                "which_card_to_replace",
            )
            self.model(tag, items, data)  # adds the model's verdict to each swap
        elif question == "best_cards":
            data, line, tool = (
                insights.best_cards(a, items),
                voice.best_cards,
                "cards_vs_other_players",
            )
        elif question == "nemesis":
            data, line, tool = (
                insights.nemesis(a, items),
                voice.nemesis,
                "opponent_cards_that_beat_me",
            )
        else:
            data = insights.best_deck(a, items) | {"proven": visuals.proven_for_player(a, items)}
            line, tool = voice.best_deck, "my_decks_compared"
        evidence = [voice.evidence(tool, data)]
        if data.get("proven"):
            evidence.append(voice.evidence("show_deck", data["proven"]))
        return {"data": data, "line": line(data), "evidence": evidence}
