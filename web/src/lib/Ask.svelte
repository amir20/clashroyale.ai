<script lang="ts">
  import { tick } from "svelte";
  import { askRook, getAnswer, getSuggestions, type ChatTurn, type Report, type Usage } from "./api";
  import Answer from "./Answer.svelte";
  import Rook, { type Mood } from "./Rook.svelte";
  import Visual from "./Visual.svelte";

  let { tag, report, onCard }: { tag: string; report: Report; onCard?: (card: any) => void } = $props();

  type Turn = { key: number; q: string; id?: string; status?: string; line?: string; data?: any; failed?: boolean; usage?: Usage; evidence?: string[]; visuals?: any[] };
  type Option = { label: string; id?: string };

  let turns = $state<Turn[]>([]);
  // svelte-ignore state_referenced_locally (a new report remounts this component)
  let builtIn = $state<Option[]>(report.questions.map((q) => ({ id: q.id, label: q.label })));
  let suggested = $state<Option[]>([]);
  let thinkingOfMore = $state(false);
  let suggestCost = $state(0);
  let suggestUsage = $state<Usage>();
  let draft = $state("");
  let busy = $state(false);
  let left = $state<number | null>(null);
  let key = 0;
  let suggestRun = 0;
  const offered = new Set<string>(); // every suggestion shown so far, so they don't come back

  const digging = ["Digging through your battles…", "Doing the math so you don't have to…", "Checking the receipts…"];
  const mood = $derived<Mood>(busy ? "thinking" : turns.at(-1)?.failed ? "oops" : turns.length ? "smug" : "idle");
  const outOfQuestions = $derived(!report.dev && left === 0);
  const options = $derived([...(outOfQuestions ? [] : suggested), ...builtIn].slice(0, 4));
  const totalCost = $derived(turns.reduce((sum, t) => sum + (t.usage?.cost_usd ?? 0), 0) + suggestCost);
  const usd = (x: number) => `$${x.toFixed(5)}`;
  const usageLine = (u: Usage) =>
    u.model
      ? `${u.model} · ${u.rounds} ${u.rounds === 1 ? "call" : "calls"} · ${u.input_tokens?.toLocaleString()} in / ${u.output_tokens?.toLocaleString()} out · ${usd(u.cost_usd)} · ${u.seconds}s`
      : `no AI used · ${usd(0)} · ${u.seconds}s`;

  function update(k: number, patch: Partial<Turn>) {
    turns = turns.map((t) => (t.key === k ? { ...t, ...patch } : t));
  }

  /** The conversation so far, for the model's memory. */
  function history(): ChatTurn[] {
    const pairs = turns.filter((t) => t.line && !t.failed && !t.status);
    return pairs
      .flatMap((t): ChatTurn[] => [
        { role: "user", content: t.q.slice(0, 1900) },
        { role: "assistant", content: t.line!.slice(0, 1900) },
      ])
      .slice(-12);
  }

  /** Start a new turn: use up the option, show the question, and bring it into view. */
  async function begin(q: string, id?: string, status = "Rook is thinking…") {
    const same = (o: Option) => o.label.trim().toLowerCase() === q.trim().toLowerCase();
    builtIn = builtIn.filter((o) => !same(o));
    suggested = suggested.filter((o) => !same(o));
    suggestRun++; // any suggestions still on their way are now out of date
    thinkingOfMore = false;
    busy = true;
    const k = ++key;
    turns = [...turns, { key: k, q, id, status }];
    await tick();
    document.getElementById(`turn-${k}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return k;
  }

  async function suggestNext() {
    if (!report.ai_enabled || outOfQuestions) return;
    const run = ++suggestRun;
    thinkingOfMore = true;
    try {
      const taken = [...new Set([...offered, ...turns.map((t) => t.q), ...builtIn.map((o) => o.label)])].slice(-40);
      const res = await getSuggestions(tag, history(), taken);
      if (run !== suggestRun) return;
      const free = new Set(builtIn.map((o) => o.label.toLowerCase()));
      suggested = res.suggestions.filter((q) => !free.has(q.toLowerCase())).map((label) => ({ label }));
      res.suggestions.forEach((q) => offered.add(q));
      if (res.usage) {
        suggestUsage = res.usage;
        suggestCost += res.usage.cost_usd;
      }
    } catch {
      // The built-in questions are still there.
    } finally {
      if (run === suggestRun) thinkingOfMore = false;
    }
  }

  async function preset(id: string, label: string) {
    if (busy) return;
    const k = await begin(label, id, digging[key % digging.length]);
    try {
      const a = await getAnswer(tag, id);
      update(k, { status: undefined, line: a.line, data: a.data, usage: a.usage, evidence: a.evidence });
    } catch {
      update(k, { status: undefined, line: "Something broke, and for once it wasn't your deck. Try again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  async function custom(q: string) {
    if (q.length < 3 || busy || outOfQuestions) return;
    const earlier = history();
    const k = await begin(q);
    try {
      await askRook(tag, q, earlier, (ev) => {
        if (ev.type === "status") update(k, { status: ev.message });
        else if (ev.type === "answer") {
          update(k, { status: undefined, line: ev.text, usage: ev.usage, evidence: ev.evidence, visuals: ev.visuals });
          left = ev.left;
        } else update(k, { status: undefined, line: ev.message, failed: true });
      });
    } catch {
      update(k, { status: undefined, line: "I lost the connection. Ask me again.", failed: true });
    }
    // Never leave a question spinning: if the stream ended without an answer, say so.
    if (turns.find((t) => t.key === k)?.status) {
      update(k, { status: undefined, line: "I lost my train of thought. Ask me again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  /** Ask from outside the chat: a goal, or a choice on a card. */
  export function ask(q: string, id?: string) {
    if (id) preset(id, q);
    else custom(q);
  }

  function pick(o: Option) {
    if (o.id) preset(o.id, o.label);
    else custom(o.label);
  }

  function submit(e: SubmitEvent) {
    e.preventDefault();
    const q = draft.trim();
    draft = "";
    custom(q);
  }
</script>

<div class="flex items-center gap-3 mb-4">
  <Rook {mood} size={56} />
  <div>
    <h2 class="text-xl">Ask Rook</h2>
    <p class="text-sm text-base-content/70">Go on. I've seen your games.</p>
  </div>
</div>

<div class="space-y-6" aria-live="polite">
  {#each turns as t (t.key)}
    <div id={`turn-${t.key}`} class="pop scroll-mt-4">
      <p class="text-sm font-semibold">{t.q}</p>
      <div class="panel mt-2 p-4">
        {#if t.status}
          <span class="flex items-center gap-2 text-sm"><span class="loading loading-dots loading-sm text-primary"></span>{t.status}</span>
        {:else}
          <p class="says">{t.line}</p>
          {#if t.id && t.data}<div class="mt-4"><Answer id={t.id} data={t.data} /></div>{/if}
          {#each t.visuals ?? [] as v, i (i)}<div class="mt-4"><Visual visual={v} {onCard} /></div>{/each}
          {#if t.evidence?.length}
            <p class="mt-4 pt-3 border-t border-base-content/10 text-xs text-base-content/70" data-testid="evidence"><span class="font-semibold text-base-content">Based on:</span> {t.evidence.join(" · ")}</p>
          {/if}
        {/if}
      </div>
      {#if t.usage}<p class="font-mono text-[11px] text-accent mt-1 px-1 break-words" data-testid="dev-usage">{usageLine(t.usage)}</p>{/if}
    </div>
  {/each}
</div>

<div class={turns.length ? "mt-6" : ""}>
  {#if turns.length && (options.length || thinkingOfMore)}
    <p class="text-sm font-medium mb-2">Ask next</p>
  {/if}
  <div class="grid gap-2" data-testid="options">
    {#each options as o (o.label)}
      <button class="panel px-4 py-3 text-left text-sm font-medium flex items-center justify-between gap-2 transition cursor-pointer hover:border-primary/60 disabled:opacity-50 disabled:cursor-not-allowed" disabled={busy} onclick={() => pick(o)}>
        <span>{o.label}</span><span class="text-primary" aria-hidden="true">→</span>
      </button>
    {/each}
  </div>
  {#if thinkingOfMore}
    <p class="flex items-center gap-2 text-sm text-base-content/70 mt-3" data-testid="thinking-more"><span class="loading loading-dots loading-xs text-primary"></span>Rook is thinking of what you should ask next…</p>
  {/if}
  {#if suggestUsage}<p class="font-mono text-[11px] text-accent mt-1 px-1 break-words" data-testid="dev-suggest-usage">suggestions: {usageLine(suggestUsage)}</p>{/if}
</div>

<form class="mt-6" onsubmit={submit}>
  <label class="text-sm font-medium" for="ask">Ask your own question</label>
  {#if report.ai_enabled}
    <div class="join w-full mt-1">
      <input id="ask" class="input join-item w-full text-base" maxlength="300" placeholder={outOfQuestions ? "That's all for today" : "Should I use Log or Zap?"} bind:value={draft} disabled={busy || outOfQuestions} />
      <button class="btn btn-primary join-item cursor-pointer disabled:cursor-not-allowed" disabled={busy || outOfQuestions || draft.trim().length < 3}>
        {#if busy}<span class="loading loading-spinner loading-sm"></span>{:else}Ask{/if}
      </button>
    </div>
    {#if report.dev}
      <p class="font-mono text-[11px] text-accent mt-1" data-testid="dev-total">dev mode · no limits · session AI cost {usd(totalCost)}</p>
    {:else}
      <p class="text-xs text-base-content/60 mt-1">{left ?? report.custom_questions_per_day} free {left === 1 ? "question" : "questions"} left today. Rook's own suggestions count; the built-in ones don't.</p>
    {/if}
  {:else}
    <p class="text-sm text-base-content/60 mt-1">Custom questions are napping for now. The ones above always work.</p>
  {/if}
</form>
