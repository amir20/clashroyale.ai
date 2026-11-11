<script lang="ts">
  import { onMount, tick } from "svelte";
  import { cleanTag, loadPlayer, type Card, type LoadEvent, type Report } from "./lib/api";
  import Ask from "./lib/Ask.svelte";
  import CardSheet from "./lib/CardSheet.svelte";
  import Rook, { type Mood } from "./lib/Rook.svelte";
  import Story from "./lib/Story.svelte";

  type Phase = "landing" | "loading" | "report";
  let phase = $state<Phase>("landing");
  let input = $state("");
  let status = $state("");
  let progress = $state(0);
  let error = $state("");
  let shaking = $state(false);
  let tag = $state("");
  // The viewer's own tag. Any other report (an opponent's) is about "them", not "you".
  let me = $state("");
  const them = $derived(!!me && tag !== me);
  let report = $state<Report | null>(null);
  let view = $state<"story" | "chat">("story");
  let chat = $state<Ask>();
  let picked = $state<Card | null>(null);
  let stop: (() => void) | undefined;

  const mood = $derived<Mood>(
    error ? "oops" : phase === "loading" ? "thinking" : phase === "report" ? "happy" : "idle",
  );
  const heroLine = $derived(
    error ||
      (phase === "loading"
        ? status
        : phase === "report"
          ? them
            ? `Here's ${report?.overview.player.name ?? "them"}.`
            : "There you are. Let's see the damage."
          : "Give me your tag and I'll tell you the truth about your deck."),
  );

  const urlFor = (t: string) => (me && t !== me ? `?tag=${t}&vs=${me}` : `?tag=${t}`);

  function fail(message: string) {
    if (them) {
      // Looking up an opponent failed: offer the viewer's own tag again, not the opponent's.
      tag = input = me;
      history.replaceState(null, "", urlFor(me));
    }
    phase = "landing";
    error = message;
    shaking = true;
    setTimeout(() => (shaking = false), 400);
  }

  async function onEvent(e: LoadEvent) {
    if (e.type === "status") {
      status = e.message;
      if (e.progress) progress = e.progress;
    } else if (e.type === "error") {
      fail(e.message);
    } else {
      report = e.report;
      view = "story";
      phase = "report";
      if (!them) localStorage.setItem("rook-tag", tag);
      history.replaceState(null, "", urlFor(tag));
      scrollTo(0, 0);
    }
  }

  /** Show the chat, optionally asking something on the player's behalf. */
  async function openChat(question?: string, id?: string) {
    view = "chat";
    await tick();
    if (question) chat?.ask(question, id);
  }

  /** Load a report. Without `as`, the tag is the viewer's own. */
  function start(raw: string, as?: string) {
    const cleaned = cleanTag(raw);
    if (cleaned.length < 3) return fail("That's too short to be a tag. Nice try.");
    stop?.();
    me = as ? cleanTag(as) : cleaned;
    tag = cleaned;
    input = cleaned;
    error = "";
    report = null;
    progress = 3;
    status = "Waking up…";
    phase = "loading";
    stop = loadPlayer(cleaned, onEvent, them);
  }

  /** Open another player's report, such as an opponent's, keeping the viewer's own tag. */
  function visit(other: string) {
    const t = cleanTag(other);
    history.pushState({ visited: true }, "", urlFor(t));
    start(t, me);
  }

  /** Back to the previous report, or to the viewer's own when the page opened on someone else. */
  function back() {
    if (history.state?.visited) return history.back();
    history.replaceState(null, "", urlFor(me));
    start(me);
  }

  function fromUrl(): boolean {
    const q = new URLSearchParams(location.search);
    const t = q.get("tag");
    if (t) start(t, q.get("vs") ?? undefined);
    return !!t;
  }

  function reset() {
    stop?.();
    phase = "landing";
    report = null;
    history.replaceState(null, "", location.pathname);
  }

  onMount(() => {
    if (!fromUrl()) input = localStorage.getItem("rook-tag") ?? "";
    const popped = () => {
      if (!fromUrl()) reset();
    };
    addEventListener("popstate", popped);
    return () => {
      stop?.();
      removeEventListener("popstate", popped);
    };
  });

  const o = $derived(report?.overview);
  // Card tips and the chat coach the viewer, so they stay on the viewer's own report.
  const tappable = $derived(report?.ai_enabled && !them ? (c: Card) => (picked = c) : undefined);
  </script>

<main class="sky min-h-svh">
  <section class="sticky top-0 h-svh flex flex-col items-center justify-center px-6 text-center" class:hero-recede={phase === "report"}>
    <Rook {mood} size={phase === "loading" ? 200 : 170} />
    <h1 class="text-5xl mt-2">Rook</h1>
    <p class="mt-3 max-w-xs min-h-14 text-lg leading-snug" class:text-error={!!error} aria-live="polite">{heroLine}</p>

    {#if phase === "loading"}
      <div class="w-full max-w-xs mt-5 pop">
        <progress class="progress progress-primary w-full h-3" value={progress} max="100"></progress>
        <p class="text-sm opacity-60 mt-2 dots">#{tag}</p>
      </div>
    {:else if phase === "landing"}
      <form class="w-full max-w-xs mt-5" class:shake={shaking} onsubmit={(e) => { e.preventDefault(); start(input); }}>
        <label class="input input-lg w-full">
          <span class="opacity-50">#</span>
          <input class="uppercase" placeholder="YOUR TAG" autocomplete="off" autocapitalize="characters" spellcheck="false" aria-label="Your player tag" bind:value={input} />
        </label>
        <button class="btn btn-primary btn-lg btn-game w-full mt-4 font-semibold">Roast my deck</button>
        <p class="text-xs opacity-60 mt-3">Find your tag under your name on your in-game profile.</p>
      </form>
    {/if}
  </section>

  {#if report && o}
    {#key report}
      <div class:hidden={view !== "story"}>
        <Story {report} {them} active={view === "story"} onCard={tappable} onGoal={(label, id) => openChat(label, id)} onAsk={() => openChat()} onReset={reset} onPlayer={visit} onBack={back} />
      </div>
      <div class="fixed inset-0 z-10 bg-base-100 overflow-y-auto" class:hidden={view !== "chat"} data-testid="chat">
        <div class="mx-auto max-w-2xl px-6 pt-4 pb-12">
          <button class="btn btn-ghost btn-sm -ml-3 mb-4 font-medium cursor-pointer" onclick={() => (view = "story")}>← Your story</button>
          <Ask bind:this={chat} {tag} {report} onCard={tappable} />
          <footer class="mt-12 text-xs text-base-content/40 text-center pb-[env(safe-area-inset-bottom)]">
            This material is unofficial and is not endorsed by Supercell. For more information see
            <a class="underline" href="https://www.supercell.com/fan-content-policy" target="_blank" rel="noreferrer">Supercell's Fan Content Policy</a>.
          </footer>
        </div>
      </div>
      {#if picked}
        <CardSheet card={picked} inDeck={o.deck.some((c) => c.id === picked?.id)} cards={report.cards} onAsk={(q) => { picked = null; openChat(q); }} onClose={() => (picked = null)} />
      {/if}
    {/key}
  {/if}
</main>
