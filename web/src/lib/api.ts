export type Card = { id: number; name: string; elixir: number | null; rarity: string | null; icon: string | null };
export type PlayedCard = Card & { status?: "evolution" | "hero" | "both" | null };
export type Battle = {
  battle_id: number;
  time: string;
  type: string | null;
  result: "win" | "loss" | "draw";
  crowns: number | null;
  opponent_crowns: number | null;
  trophy_change: number | null;
  opponent: { tag: string; name: string | null; trophies: number | null; deck: PlayedCard[] };
};
type Played = PlayedCard & { status?: "evolution" | "hero" | "both" | null };
/** The deck model's verdict on a swap: win chance after it, change in wins per 100, and why. */
export type ModelVerdict = {
  win: number;
  gain: number;
  parts: { alone: number; synergy: number; matchups: number };
  reasons: (
    | { kind: "synergy"; how: "gained" | "lost"; cards: Played[]; effect: number }
    | { kind: "matchup"; card: Played; share: number; effect: number }
  )[];
};
/** The deck model's view of the player's deck against the decks at their trophies. */
export type ModelView = {
  win: number;
  cards: (Played & { total: number; alone: number; synergy: number; matchups: number })[];
  strong: (Played & { share: number; effect: number })[];
  weak: (Played & { share: number; effect: number })[];
  synergy: { cards: Played[]; effect: number }[];
  clash: { cards: Played[]; effect: number }[];
  field: { decks: number; near: boolean; scope: string; where: string };
  /** The player's last opponents, played again on paper with their deck and the suggested one. */
  replay: {
    games: number;
    wins: number;
    expected: number;
    expected_pick: number | null;
    battles: { battle_id: number; time: string; result: "win" | "loss" | "draw"; opponent: string | null; deck: Played[]; level_gap: number; now: number; pick: number | null }[];
  } | null;
  /** The model's own suggestion: flex swaps that raise the predicted win chance most. */
  pick: {
    win: number;
    win_after: number;
    gain: number;
    out: Played[];
    in: Played[];
    steps: { out: Played; in: Played; gain: number }[];
    reasons: ModelVerdict["reasons"];
    parts: ModelVerdict["parts"];
    singles: { out: Played; in: Played; gain: number }[];
    check: { verdict: "better" | "worse" | "even" | "unknown"; gain: number | null; margin: number | null; games: number; players: number; near: boolean } | null;
  } | null;
  about: {
    published: string;
    battles: number;
    first_day: string;
    test_day: string;
    test_battles: number;
    accuracy: number;
    auc: number;
    calibration: number;
    strength_accuracy: number;
    pairs: number;
    matchups: number;
  };
};
export type Report = {
  overview: {
    player: { tag: string; name: string; trophies: number | null; clan: string | null; arena: string | null };
    deck: (Card & { level: number | null; status: "evolution" | "hero" | "both" | null })[];
    avg_elixir: number | null;
    record: { games: number; wins: number; losses: number; draws: number; win_rate: number; margin: number };
    streak: { kind: string | null; length: number };
    net_trophies: number;
    recent: string[];
  };
  intro: string;
  battles: Battle[];
  similar: { battles: number; players: number; days: number; enough: boolean; near: boolean; ranked?: boolean } | null;
  patterns: {
    close_games: { games: number; wins: number; losses: number };
    three_crown_wins: number;
    three_crowned_losses: number;
    crowns_for: number | null;
    crowns_against: number | null;
    elixir_leaked_wins: number | null;
    elixir_leaked_losses: number | null;
  };
  patterns_line: string;
  model: ModelView | null;
  story: {
    trend: any | null;
    close: any | null;
    worst: any | null;
    elixir: any | null;
    replace: any;
    surprises: (Record<string, any> & { kind: string; headline: string; detail: string })[];
    lines: { trend: string | null; close: string | null; worst: string | null; elixir: string | null; replace: string; pick?: string | null };
  };
  cards: Card[];
  questions: { id: string; label: string }[];
  custom_questions_per_day: number;
  ai_enabled: boolean;
  dev: boolean;
};
export type Usage = {
  model: string | null;
  cost_usd: number;
  seconds: number;
  rounds?: number;
  input_tokens?: number;
  output_tokens?: number;
};
/** Who's being loaded, shown while the report is built. */
export type LoadingPlayer = {
  name: string | null;
  level: number | null;
  trophies: number | null;
  best_trophies: number | null;
  wins: number | null;
  battles: number | null;
  three_crowns: number | null;
  clan: string | null;
  arena: string | null;
  mode: string | null; // their newest battle's: "pathOfLegend" (ranked), "trail" (ladder), ...
  league: number | null; // ranked league this season, 1-10
  rating: number | null;
  rank: number | null;
  deck: { name: string | null; icon: string | null }[]; // from their newest battle
};
/** A finished loading step: how long it took and how much data it went through. */
export type LoadStep = { type: "step"; step: string; label: string; seconds: number; detail: string };

export type LoadEvent =
  | { type: "status"; step: string; message: string; progress?: number; known?: boolean; player?: LoadingPlayer | null }
  | LoadStep
  | { type: "ready"; report: Report }
  | { type: "error"; code?: string; message: string };

export const cleanTag = (t: string) => t.trim().toUpperCase().replace(/^#/, "").replace(/O/g, "0");

/** Stream a player's report. `them` is for looking up someone else, such as an opponent. */
export function loadPlayer(tag: string, onEvent: (e: LoadEvent) => void, them = false): () => void {
  const source = new EventSource(`/api/players/${encodeURIComponent(tag)}/load${them ? "?them=true" : ""}`);
  let finished = false;
  source.onmessage = (m) => {
    const event = JSON.parse(m.data) as LoadEvent;
    if (event.type !== "status" && event.type !== "step") {
      finished = true;
      source.close();
    }
    onEvent(event);
  };
  source.onerror = () => {
    if (finished) return;
    source.close();
    onEvent({ type: "error", code: "network", message: "I lost the connection. Tap to try again." });
  };
  return () => source.close();
}

export async function getAnswer(tag: string, id: string): Promise<{ line: string; data: any; evidence: string[]; usage?: Usage; ref?: string }> {
  const res = await fetch(`/api/players/${encodeURIComponent(tag)}/questions/${id}`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

/** One finished step of a custom question: a model call or a lookup, with what it cost. */
export type Step =
  | { type: "step"; kind: "model"; round: number; ms: number; at_ms: number; looks_up: string[]; input_tokens: number; output_tokens: number; finish: string | null }
  | { type: "step"; kind: "tool"; name: string; label: string; args: Record<string, unknown>; ms: number; at_ms: number; bytes: number; rows: number; error: string | null };

export type AskEvent =
  | { type: "status"; message: string }
  | Step
  | { type: "delta"; round: number; text: string }
  | { type: "answer"; text: string; left: number; evidence: string[]; visuals: any[]; usage?: Usage; ref?: string }
  | { type: "error"; message: string };

/** Thumbs up (1) or down (-1) on an answer, with an optional note. Saved for improving Rook. */
export async function rateChat(ref: string, rating: 1 | -1, note?: string) {
  await fetch(`/api/chats/${encodeURIComponent(ref)}/rating`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating, note: note || null }),
  });
}

export type ChatTurn = { role: "user" | "assistant"; content: string };

export async function getSuggestions(
  tag: string,
  history: ChatTurn[],
  asked: string[],
): Promise<{ suggestions: string[]; usage?: Usage }> {
  const res = await fetch(`/api/players/${encodeURIComponent(tag)}/suggestions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ history, asked }),
  });
  if (!res.ok) return { suggestions: [] };
  return res.json();
}

export async function askRook(
  tag: string,
  question: string,
  history: ChatTurn[],
  onEvent: (e: AskEvent) => void,
  signal?: AbortSignal,
) {
  const res = await fetch(`/api/players/${encodeURIComponent(tag)}/ask`, {
    signal,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, history }),
  });
  if (res.status === 429) return onEvent({ type: "error", message: "That's your questions for today. Come back tomorrow, I need my beauty sleep." });
  if (!res.ok || !res.body) return onEvent({ type: "error", message: "My brain's offline. Try one of the questions above." });
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const p of parts) if (p.startsWith("data:")) onEvent(JSON.parse(p.slice(5)));
  }
}
