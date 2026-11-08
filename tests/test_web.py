"""The web API with fake game and OpenRouter servers: no network."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from conftest import ME, battles, profile
from fastapi.testclient import TestClient

from crai import coach, insights
from crai.analytics import Analyzer
from crai.config import Settings
from crai.web import DAILY_QUESTIONS, LOADS_PER_MINUTE, _finished_text, _tools, create_app

NEW = "#Q9Q"


def events(body: str) -> list[dict[str, Any]]:
    return [json.loads(b[5:]) for b in body.split("\n\n") if b.startswith("data:")]


def game(request: httpx.Request) -> httpx.Response:
    path = unquote(request.url.path)
    if path.endswith("/#UUU") or "/#UUU/" in path:
        return httpx.Response(404)
    if path.endswith("/battlelog"):
        return httpx.Response(200, json=battles(12))
    return httpx.Response(200, json=profile(ME if ME in path else NEW))


def client(db_path: Path, handler: Any = game, key: str | None = None) -> TestClient:
    settings = Settings(("t",), "https://api.test/v1", db_path, None, key)
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def test_never_seen_player_reports_every_step(db_path: Path) -> None:
    with client(db_path) as c:
        out = events(c.get(f"/api/players/{ME[1:]}/load").text)
    assert [e["step"] for e in out if e["type"] == "status"][:2] == ["lookup", "fetch"]
    assert all(e["message"] for e in out if e["type"] == "status")
    ready = out[-1]
    assert ready["type"] == "ready"
    assert ready["report"]["overview"]["player"]["tag"] == ME
    assert ready["report"]["intro"]


@pytest.mark.parametrize(
    ("tag", "code"), [("ABC", "bad_tag"), ("UUU", "not_found")], ids=["invalid", "missing"]
)
def test_load_errors_are_friendly(db_path: Path, tag: str, code: str) -> None:
    with client(db_path) as c:
        out = events(c.get(f"/api/players/{tag}/load").text)
    assert out[-1]["type"] == "error"
    assert out[-1]["code"] == code


def test_load_rate_limit(db_path: Path) -> None:
    with client(db_path) as c:
        for _ in range(LOADS_PER_MINUTE):
            c.get(f"/api/players/{ME[1:]}/load")
        assert events(c.get(f"/api/players/{ME[1:]}/load").text)[-1]["code"] == "rate_limited"


@pytest.mark.parametrize("q", ["replace_card", "best_cards", "nemesis", "best_deck"])
def test_prefilled_questions_answer_in_character(db_path: Path, q: str) -> None:
    with client(db_path) as c:
        body = c.get(f"/api/players/{ME[1:]}/questions/{q}").json()
    assert body["line"]
    assert "data" in body


def test_small_samples_are_never_called_real(db_path: Path) -> None:
    with client(db_path) as c:
        data = c.get(f"/api/players/{ME[1:]}/questions/nemesis").json()["data"]
    assert all(not r["real"] for r in [*data["worst"], *data["best"]] if r["games"] < 15)


def ai(request: httpx.Request) -> httpx.Response:
    sent = json.loads(request.content)
    if sent["messages"][-1]["role"] == "tool":
        reply: dict[str, Any] = {"role": "assistant", "content": "Your deck. Tragic."}
    else:
        call = {"id": "1", "function": {"name": "get_overview", "arguments": "{}"}}
        reply = {"role": "assistant", "tool_calls": [call]}
    return httpx.Response(200, json={"choices": [{"message": reply}]})


def test_custom_question_uses_tools_then_answers(db_path: Path) -> None:
    with client(db_path, ai, "k") as c:
        out = events(c.post(f"/api/players/{ME[1:]}/ask", json={"question": "How am I?"}).text)
    assert any("your overview" in e.get("message", "") for e in out)
    assert out[-1] == {
        "type": "answer",
        "text": "Your deck. Tragic.",
        "left": DAILY_QUESTIONS - 1,
        "evidence": ["Your last 200 games"],
    }


def test_custom_questions_have_a_daily_limit(db_path: Path) -> None:
    with client(db_path, ai, "k") as c:
        for _ in range(DAILY_QUESTIONS):
            assert (
                c.post(f"/api/players/{ME[1:]}/ask", json={"question": "Why?"}).status_code == 200
            )
        assert c.post(f"/api/players/{ME[1:]}/ask", json={"question": "Why?"}).status_code == 429


def test_ai_failure_still_ends_with_a_message(db_path: Path) -> None:
    down = lambda _: httpx.Response(500)  # noqa: E731
    with client(db_path, down, "k") as c:
        out = events(c.post(f"/api/players/{ME[1:]}/ask", json={"question": "Why?"}).text)
    assert out[-1]["type"] == "error"


def test_ai_disabled_without_a_key(db_path: Path) -> None:
    with client(db_path) as c:
        assert c.post(f"/api/players/{ME[1:]}/ask", json={"question": "Why?"}).status_code == 503


def dev_client(db_path: Path, handler: Any, key: str | None = "k") -> TestClient:
    settings = Settings(("t",), "https://api.test/v1", db_path, None, key, dev=True)
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def priced_ai(request: httpx.Request) -> httpx.Response:
    response = json.loads(ai(request).content)
    response["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0002}
    return httpx.Response(200, json=response)


def test_dev_mode_reports_usage_and_has_no_limit(db_path: Path) -> None:
    with dev_client(db_path, priced_ai) as c:
        for _ in range(DAILY_QUESTIONS + 2):
            res = c.post(f"/api/players/{ME[1:]}/ask", json={"question": "How am I?"})
            assert res.status_code == 200
        usage = events(res.text)[-1]["usage"]
        computed = c.get(f"/api/players/{ME[1:]}/questions/best_deck").json()["usage"]
    assert usage["rounds"] == 2
    assert usage["input_tokens"] == 200
    assert usage["cost_usd"] == pytest.approx(0.0004)
    assert computed["cost_usd"] == 0


def test_production_never_exposes_usage(db_path: Path) -> None:
    with client(db_path, priced_ai, "k") as c:
        out = events(c.post(f"/api/players/{ME[1:]}/ask", json={"question": "How am I?"}).text)
        computed = c.get(f"/api/players/{ME[1:]}/questions/best_deck").json()
    assert "usage" not in out[-1]
    assert "usage" not in computed


def test_every_tool_has_a_prompt_and_an_implementation(db_path: Path) -> None:
    with client(db_path) as c:
        svc = c.app.state.service  # ty: ignore[unresolved-attribute]
        assert set(coach.tool_prompts()) == set(_tools(svc, ME))
    assert "Rook" in coach.system_prompt()


def test_record_against_any_card(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    faced = insights.record_against(analyzer, items, "pekka")
    assert faced["card"] == "P.E.K.K.A"
    assert faced["games"] == 200
    never = insights.record_against(analyzer, items, "x-bow")
    assert never == {"card": "X-Bow", "games": 0, "total_games_checked": 200}


@pytest.mark.parametrize("q", ["replace_card", "best_cards", "nemesis", "best_deck"])
def test_answers_are_plain_language_and_show_their_evidence(db_path: Path, q: str) -> None:
    with client(db_path) as c:
        body = c.get(f"/api/players/{ME[1:]}/questions/{q}").json()
    assert "±" not in body["line"]
    assert "points" not in body["line"]
    assert body["evidence"][0]


def test_first_ai_turn_must_use_data(db_path: Path) -> None:
    choices = []

    def spy(request: httpx.Request) -> httpx.Response:
        choices.append(json.loads(request.content)["tool_choice"])
        return ai(request)

    with client(db_path, spy, "k") as c:
        c.post(f"/api/players/{ME[1:]}/ask", json={"question": "How am I?"})
    assert choices == ["required", "auto"]


@pytest.mark.parametrize(
    ("content", "finish", "expected"),
    [
        ("All good. Keep going.", "stop", "All good. Keep going."),
        ("Keep Ronin. It wins more. But the next", "length", "Keep Ronin. It wins more."),
        ("Cut off with no sentence end", "length", ""),
        (None, "length", ""),
    ],
)
def test_finished_text(content: str | None, finish: str, expected: str) -> None:
    assert _finished_text(content, finish) == expected


def test_empty_model_reply_is_never_shown_blank(db_path: Path) -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["messages"][-1]["role"] != "tool":
            return ai(request)
        reply = {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
        return httpx.Response(200, json={"choices": [reply]})

    with client(db_path, empty, "k") as c:
        out = events(c.post(f"/api/players/{ME[1:]}/ask", json={"question": "Why?"}).text)
    assert out[-1]["type"] == "error"
    assert out[-1]["message"]


def test_parse_suggestions_keeps_only_clean_new_questions() -> None:
    text = """1. How do I do against Hog?
    - What pairs with Ronin?
    not a question
    Which card should I replace?
    Too?
    Is Witch good here?
    One more valid question here?"""
    assert coach.parse_suggestions(text, ["which card should i replace?"]) == [
        "How do I do against Hog?",
        "What pairs with Ronin?",
        "Is Witch good here?",
    ]
    assert coach.parse_suggestions(None, []) == []


def suggester(request: httpx.Request) -> httpx.Response:
    content = "How do I do against Hog Rider?\nWhat pairs with Ronin?\nWhich deck wins most?"
    reply = {"message": {"role": "assistant", "content": content}}
    return httpx.Response(200, json={"choices": [reply], "usage": {"cost": 0.0001}})


def test_model_suggests_follow_ups(db_path: Path) -> None:
    with dev_client(db_path, suggester) as c:
        body = c.post(f"/api/players/{ME[1:]}/suggestions", json={"asked": ["Why?"]}).json()
    assert len(body["suggestions"]) == 3
    assert body["usage"]["cost_usd"] == pytest.approx(0.0001)


def test_suggestions_fail_quietly(db_path: Path) -> None:
    with client(db_path, lambda _: httpx.Response(500), "k") as c:
        assert c.post(f"/api/players/{ME[1:]}/suggestions", json={}).json() == {"suggestions": []}
    with client(db_path) as c:  # no AI key
        assert c.post(f"/api/players/{ME[1:]}/suggestions", json={}).json() == {"suggestions": []}


def test_chat_history_reaches_the_model(db_path: Path) -> None:
    seen: list[Any] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"])
        return ai(request)

    history = [
        {"role": "user", "content": "Is Ronin good?"},
        {"role": "assistant", "content": "It is fine."},
    ]
    with client(db_path, spy, "k") as c:
        c.post(f"/api/players/{ME[1:]}/ask", json={"question": "And Zap?", "history": history})
    assert [m["role"] for m in seen[0]] == ["system", "user", "assistant", "user"]
    assert seen[0][1]["content"] == "Is Ronin good?"
    assert seen[0][-1]["content"] == "And Zap?"


def test_overview_shows_similar_battles_and_card_status(db_path: Path) -> None:
    with client(db_path) as c:
        report = events(c.get(f"/api/players/{ME[1:]}/load").text)[-1]["report"]
    assert set(report["similar"]) == {"battles", "players", "days", "enough"}
    assert all("status" in card for card in report["overview"]["deck"])


@pytest.mark.parametrize(
    ("level", "status", "art"),
    [(1, "evolution", "evo"), (2, "hero", "hero"), (None, None, "base")],
)
def test_cards_show_how_they_were_played(level: int | None, status: str | None, art: str) -> None:
    card = {"icon": "base", "evo_icon": "evo", "hero_icon": "hero"}
    played = insights._played_as(card, level)
    assert (played["status"], played["icon"]) == (status, art)
