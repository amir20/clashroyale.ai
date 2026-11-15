<script lang="ts">
  import { onDestroy, tick } from "svelte";
  import { askRook, getAnswer, getSuggestions, rateChat, type ChatTurn, type Report, type Step, type Usage } from "./api";
  import Answer from "./Answer.svelte";
  import Icon from "./Icon.svelte";
  import Rook, { type Mood } from "./Rook.svelte";
  import Visual from "./Visual.svelte";

  let { tag, report, onCard, onBack }: { tag: string; report: Report; onCard?: (card: any) => void; onBack?: () => void } = $props();

  type Turn = {
    key: number;
    q: string;
    id?: string;
    status?: string;
    line?: string;
    data?: any;
    failed?: boolean;
    usage?: Usage;
    evidence?: string[];
    visuals?: any[];
    started: number;
    seconds?: number;
    steps: Step[];
    ref?: string;
    rating?: 1 | -1;
    note?: string;
    noteSent?: boolean;
    showSteps?: boolean;
    draft?: string; // the reply as it streams in
    copied?: boolean;
  };
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
  let now = $state(performance.now());
  let key = 0;
  let suggestRun = 0;
  let controller = $state<AbortController>();
  let box = $state<HTMLTextAreaElement>();
  const offered = new Set<string>(); // every suggestion shown so far, so they don't come back

  // One clock for the running timer; it only ticks while Rook is working.
  let clock: ReturnType<typeof setInterval> | undefined;
  $effect(() => {
    if (busy && !clock) clock = setInterval(() => (now = performance.now()), 100);
    if (!busy && clock) {
      clearInterval(clock);
      clock = undefined;
    }
  });
  onDestroy(() => clearInterval(clock));

  const digging = ["Digging through your battles…", "Doing the math so you don't have to…", "Checking the receipts…"];
  const mood = $derived<Mood>(busy ? "thinking" : turns.at(-1)?.failed ? "oops" : turns.length ? "smug" : "idle");
  const outOfQuestions = $derived(!report.dev && left === 0);
  // Suggestions first, then the built-in questions, each label once (a card's context can offer
  // a built-in question too).
  const options = $derived(
    [...(outOfQuestions ? suggested.filter((o) => o.id) : suggested), ...builtIn]
      .filter((o, i, all) => all.findIndex((x) => x.label.toLowerCase() === o.label.toLowerCase()) === i)
      .slice(0, 4),
  );
  const totalCost = $derived(turns.reduce((sum, t) => sum + (t.usage?.cost_usd ?? 0), 0) + suggestCost);
  const usd = (x: number) => `$${x.toFixed(5)}`;
  const usageLine = (u: Usage) =>
    u.model
      ? `${u.model} · ${u.rounds} ${u.rounds === 1 ? "call" : "calls"} · ${u.input_tokens?.toLocaleString()} in / ${u.output_tokens?.toLocaleString()} out · ${usd(u.cost_usd)} · ${u.seconds}s`
      : `no AI used · ${usd(0)} · ${u.seconds}s`;

  const secs = (ms: number) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)}s`);
  const size = (bytes: number) => (bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`);
  const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const elapsed = (t: Turn) => (t.seconds ?? (now - t.started) / 1000).toFixed(1);
  const lookups = (t: Turn) => t.steps.filter((s) => s.kind === "tool") as Extract<Step, { kind: "tool" }>[];

  /** One step, in plain words: what Rook did and what came back. */
  function describe(s: Step, first: boolean): { title: string; detail: string } {
    if (s.kind === "tool") {
      const detail = s.error ? `Failed: ${s.error}` : `${s.rows ? `${plural(s.rows, "record")} · ` : ""}${size(s.bytes)}`;
      return { title: s.label.charAt(0).toUpperCase() + s.label.slice(1), detail };
    }
    const detail = report.dev ? `${s.input_tokens.toLocaleString()} tokens in · ${s.output_tokens.toLocaleString()} out` : "";
    const verb = first ? "Read your question" : "Read what it found";
    const n = s.looks_up.length;
    const next = n ? ` and picked ${n === 1 ? "1 thing" : `${n} things`} to look up` : " and wrote the answer";
    return { title: verb + next, detail };
  }

  function update(k: number, patch: Partial<Turn>) {
    turns = turns.map((t) => (t.key === k ? { ...t, ...patch } : t));
  }

  function finish(k: number, patch: Partial<Turn>) {
    const t = turns.find((x) => x.key === k);
    update(k, { status: undefined, seconds: t ? (performance.now() - t.started) / 1000 : undefined, ...patch });
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
    now = performance.now();
    const k = ++key;
    turns = [...turns, { key: k, q, id, status, started: now, steps: [] }];
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
      finish(k, { line: a.line, data: a.data, usage: a.usage, evidence: a.evidence, ref: a.ref });
    } catch {
      finish(k, { line: "Something broke, and for once it wasn't your deck. Try again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  async function custom(q: string) {
    if (q.length < 3 || busy || outOfQuestions) return;
    const earlier = history();
    const k = await begin(q);
    controller = new AbortController();
    const turn = () => turns.find((t) => t.key === k);
    try {
      await askRook(tag, q, earlier, (ev) => {
        if (ev.type === "status") update(k, { status: ev.message });
        else if (ev.type === "delta") update(k, { draft: (turn()?.draft ?? "") + ev.text, status: "Writing…" });
        else if (ev.type === "step") {
          // Text written before a lookup was the model thinking out loud, not the answer.
          const thinking = ev.kind === "model" && ev.looks_up.length > 0;
          update(k, { steps: [...(turn()?.steps ?? []), ev], ...(thinking ? { draft: undefined } : {}) });
        } else if (ev.type === "answer") {
          finish(k, { line: ev.text, draft: undefined, usage: ev.usage, evidence: ev.evidence, visuals: ev.visuals, ref: ev.ref });
          left = ev.left;
        } else finish(k, { line: ev.message, draft: undefined, failed: true });
      }, controller.signal);
    } catch {
      if (controller.signal.aborted) finish(k, { line: turn()?.draft || "Stopped.", draft: undefined, failed: !turn()?.draft });
      else finish(k, { line: "I lost the connection. Ask me again.", draft: undefined, failed: true });
    }
    controller = undefined;
    // Never leave a question spinning: if the stream ended without an answer, say so.
    if (turns.find((t) => t.key === k)?.status) {
      finish(k, { line: "I lost my train of thought. Ask me again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  function stop() {
    controller?.abort();
  }

  async function copy(t: Turn) {
    try {
      await navigator.clipboard.writeText(t.line ?? "");
      update(t.key, { copied: true });
      setTimeout(() => update(t.key, { copied: false }), 1500);
    } catch {
      // Clipboard blocked: nothing to do.
    }
  }

  /** The box grows with what's typed, up to a few lines. */
  function grow() {
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 140)}px`;
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      box?.form?.requestSubmit();
    }
  }

  function rate(t: Turn, rating: 1 | -1) {
    if (!t.ref || t.rating === rating) return;
    update(t.key, { rating });
    rateChat(t.ref, rating).catch(() => {});
  }

  function sendNote(e: SubmitEvent, t: Turn) {
    e.preventDefault();
    if (!t.ref || !t.note?.trim()) return;
    update(t.key, { noteSent: true });
    rateChat(t.ref, -1, t.note.trim()).catch(() => {});
  }

  /** Ask from outside the chat: a goal, or a choice on a card. */
  export function ask(q: string, id?: string) {
    if (id) preset(id, q);
    else custom(q);
  }

  /** Questions about what they were looking at, first among the suggestions. */
  export function offer(context: Option[]) {
    if (busy) return;
    const fresh = context.filter((c) => !turns.some((t) => t.q.toLowerCase() === c.label.toLowerCase()));
    const labels = new Set(fresh.map((c) => c.label.toLowerCase()));
    suggested = [...fresh, ...suggested.filter((o) => !labels.has(o.label.toLowerCase()))];
  }

  /** Put the cursor in the box, ready to type. */
  export function focus() {
    box?.focus({ preventScroll: true });
  }

  function pick(o: Option) {
    if (o.id) preset(o.id, o.label);
    else custom(o.label);
  }

  function submit(e: SubmitEvent) {
    e.preventDefault();
    const q = draft.trim();
    if (q.length < 3) return;
    draft = "";
    requestAnimationFrame(grow);
    custom(q);
  }
</script>

<div class="flex min-h-svh flex-col">
  <header class="sticky top-0 z-10 -mx-4 flex items-center gap-2 bg-base-100/95 px-2 pt-[env(safe-area-inset-top)] backdrop-blur-md">
    <button class="grid h-11 w-11 shrink-0 cursor-pointer place-items-center rounded-full hover:bg-base-200" aria-label="Back to your story" onclick={() => onBack?.()}>
      <Icon name="back" class="h-5 w-5" />
    </button>
    <div class="flex min-w-0 items-center gap-2.5 py-2">
      <div class="grid h-9 w-9 shrink-0 place-items-center overflow-hidden rounded-full bg-base-200"><Rook {mood} size={34} /></div>
      <div class="min-w-0 leading-tight">
        <h2 class="text-lg">Rook</h2>
        <p class="truncate text-xs text-base-content/60">{busy ? "Working on it…" : "Your deck coach"}</p>
      </div>
    </div>
  </header>

  <div class="flex-1 pt-4" aria-live="polite">
    {#if !turns.length}
      <div class="flex flex-col items-center py-8 text-center">
        <Rook {mood} size={96} />
        <p class="game mt-3 text-2xl">Ask me anything</p>
        <p class="mt-1 text-sm text-base-content/60">Go on. I've seen your games.</p>
      </div>
    {/if}

    <div class="space-y-7">
      {#each turns as t (t.key)}
        <article id={`turn-${t.key}`} class="scroll-mt-20 space-y-3">
          <div class="flex justify-end">
            <p class="max-w-[85%] whitespace-pre-wrap rounded-3xl bg-base-300 px-4 py-2.5 text-[15px] leading-snug">{t.q}</p>
          </div>

          <div class="space-y-3">
            <div class="flex items-center gap-2">
              <div class="grid h-7 w-7 shrink-0 place-items-center overflow-hidden rounded-full bg-base-200"><Rook mood={t.status ? "thinking" : t.failed ? "oops" : "idle"} size={26} /></div>
              <span class="text-sm font-semibold">Rook</span>
            </div>
            <div class="min-w-0 space-y-3">
              {#if t.status || t.steps.length}
                <!-- What Rook is doing, live; folded away once there's an answer. -->
                {@const open = !!t.status || !!t.showSteps}
                <div class="rounded-2xl bg-base-200/60 text-sm" data-testid="work">
                  <button class="flex w-full items-center gap-2 px-3.5 py-2.5 text-left {t.status ? 'cursor-default' : 'cursor-pointer'}" aria-expanded={open} disabled={!!t.status} onclick={() => update(t.key, { showSteps: !t.showSteps })}>
                    {#if t.status}
                      <span class="loading loading-ring loading-xs shrink-0 text-primary"></span>
                      <span class="min-w-0 flex-1 truncate">{t.status}</span>
                    {:else}
                      <Icon name="check" class="h-4 w-4 shrink-0 text-success" />
                      <span class="min-w-0 flex-1 truncate text-base-content/70">
                        {#if lookups(t).length}{plural(lookups(t).length, "lookup")} · {size(lookups(t).reduce((n, s) => n + s.bytes, 0))}{:else}Done{/if}
                      </span>
                    {/if}
                    <span class="shrink-0 font-mono text-xs tabular-nums text-base-content/60" data-testid="timer">{elapsed(t)}s</span>
                    {#if !t.status && t.steps.length}<Icon name="chevron" class="h-4 w-4 shrink-0 text-base-content/50 transition {open ? 'rotate-180' : ''}" />{/if}
                  </button>
                  {#if open && t.steps.length}
                    <ol class="space-y-2.5 px-3.5 pb-3 pt-0.5" data-testid="steps">
                      {#each t.steps as s, i (i)}
                        {@const d = describe(s, i === 0)}
                        <li class="flex items-start gap-2.5">
                          <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full {s.kind === 'tool' && s.error ? 'bg-error' : s.kind === 'tool' ? 'bg-accent' : 'bg-primary'}"></span>
                          <div class="min-w-0 flex-1">
                            <div class="leading-snug">{d.title}</div>
                            {#if d.detail}<div class="text-xs text-base-content/55 tabular-nums">{d.detail}</div>{/if}
                          </div>
                          <span class="pt-0.5 font-mono text-xs tabular-nums text-base-content/50">{secs(s.ms)}</span>
                        </li>
                      {/each}
                    </ol>
                  {/if}
                </div>
              {/if}

              {#if t.status && t.draft}
                <p class="whitespace-pre-wrap text-[16px] leading-relaxed" data-testid="streaming">{t.draft}<span class="caret"></span></p>
              {/if}
              {#if !t.status}
                <p class="whitespace-pre-wrap text-[16px] leading-relaxed {t.failed ? 'text-error' : ''}" data-testid="reply">{t.line}</p>
                {#if t.id && t.data}<div class="rounded-2xl bg-base-200/60 p-4"><Answer id={t.id} data={t.data} /></div>{/if}
                {#each t.visuals ?? [] as v, i (i)}<div class="rounded-2xl bg-base-200/60 p-4"><Visual visual={v} {onCard} /></div>{/each}

                {#if t.evidence?.length}
                  <p class="text-xs text-base-content/55" data-testid="evidence">Based on: {t.evidence.join(" · ")}</p>
                {/if}
                <div class="-ml-2 flex items-center gap-0.5">
                  {#if !t.failed}
                    <button class="grid h-8 w-8 cursor-pointer place-items-center rounded-full text-base-content/45 transition hover:bg-base-200 hover:text-base-content" aria-label="Copy answer" onclick={() => copy(t)}>
                      <Icon name={t.copied ? "check" : "copy"} class="h-4 w-4" />
                    </button>
                  {/if}
                  {#if t.ref && !t.failed}
                    <div class="flex items-center gap-0.5" data-testid="rating">
                      <button class="grid h-8 w-8 cursor-pointer place-items-center rounded-full transition hover:bg-base-200 {t.rating === 1 ? 'text-success' : 'text-base-content/45'}" aria-label="Good answer" aria-pressed={t.rating === 1} onclick={() => rate(t, 1)}>
                        <Icon name="thumb" class="h-4 w-4" />
                      </button>
                      <button class="grid h-8 w-8 cursor-pointer place-items-center rounded-full transition hover:bg-base-200 {t.rating === -1 ? 'text-error' : 'text-base-content/45'}" aria-label="Bad answer" aria-pressed={t.rating === -1} onclick={() => rate(t, -1)}>
                        <Icon name="thumb" class="h-4 w-4 rotate-180" />
                      </button>
                    </div>
                  {/if}
                </div>
                {#if t.rating === -1}
                  {#if t.noteSent}
                    <p class="px-1 text-xs text-base-content/60">Thanks. I'll do better.</p>
                  {:else}
                    <form class="flex items-center gap-2 rounded-full bg-base-200 py-1 pl-4 pr-1" onsubmit={(e) => sendNote(e, t)}>
                      <input class="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-base-content/45" maxlength="1000" placeholder="What went wrong? (optional)" value={t.note ?? ""} oninput={(e) => update(t.key, { note: e.currentTarget.value })} />
                      <button class="h-8 cursor-pointer rounded-full bg-base-300 px-3 text-xs font-medium disabled:opacity-40" disabled={!t.note?.trim()}>Send</button>
                    </form>
                  {/if}
                {/if}
                {#if t.usage}<p class="break-words px-1 font-mono text-[11px] text-accent" data-testid="dev-usage">{usageLine(t.usage)}</p>{/if}
              {/if}
            </div>
          </div>
        </article>
      {/each}
    </div>

    {#if options.length || thinkingOfMore}
      <div class="mt-7">
        {#if turns.length}<p class="mb-2 px-1 text-xs font-medium uppercase tracking-wide text-base-content/50">Ask next</p>{/if}
        <div class="flex flex-wrap gap-2 {turns.length ? '' : 'justify-center'}" data-testid="options">
          {#each options as o (o.label)}
            <button class="cursor-pointer rounded-full bg-base-200 px-4 py-2 text-left text-sm transition hover:bg-base-300 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40" disabled={busy} onclick={() => pick(o)}>{o.label}</button>
          {/each}
        </div>
        {#if thinkingOfMore}
          <p class="mt-3 flex items-center gap-2 px-1 text-xs text-base-content/55" data-testid="thinking-more"><span class="loading loading-dots loading-xs text-primary"></span>Thinking of what you should ask next…</p>
        {/if}
        {#if suggestUsage}<p class="mt-1 break-words px-1 font-mono text-[11px] text-accent" data-testid="dev-suggest-usage">suggestions: {usageLine(suggestUsage)}</p>{/if}
      </div>
    {/if}
  </div>

  <form class="sticky bottom-0 -mx-4 mt-6 bg-gradient-to-t from-base-100 from-70% to-transparent px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-4" onsubmit={submit}>
    {#if report.ai_enabled}
      <div class="flex items-end gap-2 rounded-[26px] bg-base-200 py-1.5 pl-5 pr-1.5 transition focus-within:bg-base-300">
        <label class="sr-only" for="ask">Ask your own question</label>
        <textarea id="ask" rows="1" bind:this={box} class="composer min-w-0 flex-1 resize-none bg-transparent py-2 text-base leading-6 placeholder:text-base-content/45 disabled:cursor-not-allowed" maxlength="300" autocomplete="off" placeholder={outOfQuestions ? "That's all for today" : "Ask Rook about your deck"} bind:value={draft} oninput={grow} onkeydown={onKey} disabled={outOfQuestions}></textarea>
        {#if busy && controller}
          <button type="button" class="grid h-10 w-10 shrink-0 cursor-pointer place-items-center self-end rounded-full bg-base-content text-base-100 transition active:scale-90" aria-label="Stop" onclick={stop}>
            <Icon name="stop" class="h-4 w-4 fill-current" />
          </button>
        {:else}
          <button class="grid h-10 w-10 shrink-0 cursor-pointer place-items-center self-end rounded-full bg-primary text-primary-content transition active:scale-90 disabled:cursor-not-allowed disabled:bg-base-content/10 disabled:text-base-content/35" aria-label="Ask" disabled={busy || outOfQuestions || draft.trim().length < 3}>
            {#if busy}<span class="loading loading-spinner loading-sm"></span>{:else}<Icon name="send" class="h-5 w-5" />{/if}
          </button>
        {/if}
      </div>
      {#if report.dev}
        <p class="mt-1.5 px-2 text-center font-mono text-[11px] text-accent" data-testid="dev-total">dev mode · no limits · session AI cost {usd(totalCost)}</p>
      {:else}
        <p class="mt-1.5 px-2 text-center text-xs text-base-content/50">{left ?? report.custom_questions_per_day} free {left === 1 ? "question" : "questions"} left today · the built-in ones are free</p>
      {/if}
    {:else}
      <p class="rounded-full bg-base-200 px-5 py-3 text-center text-sm text-base-content/60">Custom questions are napping for now. The ones above always work.</p>
    {/if}
  </form>
</div>
