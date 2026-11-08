export type Card = { id: number; name: string; elixir: number | null; rarity: string | null; icon: string | null };
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
  similar: { battles: number; players: number; days: number; enough: boolean } | null;
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
export type LoadEvent =
  | { type: "status"; step: string; message: string; progress?: number; known?: boolean }
  | { type: "ready"; report: Report }
  | { type: "error"; code?: string; message: string };

export const cleanTag = (t: string) => t.trim().toUpperCase().replace(/^#/, "").replace(/O/g, "0");

export function loadPlayer(tag: string, onEvent: (e: LoadEvent) => void): () => void {
  const source = new EventSource(`/api/players/${encodeURIComponent(tag)}/load`);
  let finished = false;
  source.onmessage = (m) => {
    const event = JSON.parse(m.data) as LoadEvent;
    if (event.type !== "status") {
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

export async function getAnswer(tag: string, id: string): Promise<{ line: string; data: any; evidence: string[]; usage?: Usage }> {
  const res = await fetch(`/api/players/${encodeURIComponent(tag)}/questions/${id}`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

export type AskEvent =
  | { type: "status"; message: string }
  | { type: "answer"; text: string; left: number; evidence: string[]; usage?: Usage }
  | { type: "error"; message: string };

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
) {
  const res = await fetch(`/api/players/${encodeURIComponent(tag)}/ask`, {
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
