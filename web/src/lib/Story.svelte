<script lang="ts">
  import { cubicOut } from "svelte/easing";
  import { fly } from "svelte/transition";
  import type { Card, Report } from "./api";
  import CardTile from "./CardTile.svelte";
  import Icon, { type IconName } from "./Icon.svelte";
  import Rook from "./Rook.svelte";
  import Visual from "./Visual.svelte";

  // The first thing a player sees: their games as a few cards to swipe through, no typing needed.
  let {
    report,
    active,
    onCard,
    onGoal,
    onAsk,
    onReset,
  }: {
    report: Report;
    active: boolean;
    onCard?: (card: Card) => void;
    onGoal: (label: string, id: string) => void;
    onAsk: () => void;
    onReset: () => void;
  } = $props();

  const o = $derived(report.overview);
  const s = $derived(report.story);
  const slides = $derived([
    "you",
    ...(s.surprises?.length ? ["wow"] : []),
    ...(o.deck.length ? ["deck"] : []),
    ...(s.trend ? ["trend"] : []),
    ...(s.close ? ["close"] : []),
    ...(s.worst ? ["worst"] : []),
    ...(s.elixir ? ["elixir"] : []),
    ...(s.replace.enough ? ["try"] : []),
    "goals",
  ]);
  const goals: { title: string; detail: string; id: string; label: string; icon: IconName }[] = [
    { title: "Win more", detail: "What's actually costing me games", id: "why_losing", label: "Why do I keep losing?", icon: "trophy" },
    { title: "Find a better deck", detail: "One proven at my trophies", id: "best_deck", label: "What's my best deck?", icon: "cards" },
    { title: "Fix my deck", detail: "Which card to change", id: "replace_card", label: "Which card should I replace?", icon: "swap" },
    { title: "Beat what's beating me", detail: "The cards giving me trouble", id: "nemesis", label: "Which cards beat me the most?", icon: "shield" },
  ];
  // Card art that makes a goal concrete: the card that beats them, or the card to swap.
  const goalArt = (id: string): Card | undefined =>
    id === "nemesis" ? s.worst?.rows?.[0] : id === "replace_card" ? s.replace?.weakest ?? undefined : undefined;
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const evolved = $derived(o.deck.filter((c) => c.status));

  const WIDE = "(min-width: 1024px)";
  let wide = $state(matchMedia(WIDE).matches);
  $effect(() => {
    const query = matchMedia(WIDE);
    const change = () => (wide = query.matches);
    query.addEventListener("change", change);
    return () => query.removeEventListener("change", change);
  });
  // On a wide screen the cards pair up into sections you scroll through.
  const sectionNames: Record<string, string> = {
    you: "Overview",
    wow: "Surprises",
    deck: "Deck",
    trend: "Form",
    close: "Close games",
    worst: "Matchups",
    elixir: "Deck cost",
    try: "Next steps",
    goals: "Next steps",
  };
  const sections = $derived(Array.from({ length: Math.ceil(slides.length / 2) }, (_, i) => slides.slice(i * 2, i * 2 + 2)));

  let index = $state(0);
  let direction = $state(1);
  let downAt: { x: number; y: number } | null = null;
  const current = $derived(slides[index]);
  const last = $derived(index === slides.length - 1);
  const thin = $derived(!s.trend);

  function go(to: number) {
    if (to < 0 || to >= slides.length) return;
    direction = to > index ? 1 : -1;
    index = to;
  }

  function pointerDown(e: PointerEvent) {
    downAt = (e.target as Element).closest("button, a, input") ? null : { x: e.clientX, y: e.clientY };
  }

  /** Swipe either way, or tap the left third to go back and the rest to go on. */
  function pointerUp(e: PointerEvent) {
    if (!downAt) return;
    const dx = e.clientX - downAt.x;
    const dy = e.clientY - downAt.y;
    downAt = null;
    if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) go(index + (dx < 0 ? 1 : -1));
    else if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
      const box = (e.currentTarget as Element).getBoundingClientRect();
      go(index + (e.clientX - box.left < box.width / 3 ? -1 : 1));
    }
  }

  function key(e: KeyboardEvent) {
    if (!active || wide || document.querySelector("[data-testid=card-sheet]")) return;
    if (e.key === "ArrowRight") go(index + 1);
    if (e.key === "ArrowLeft") go(index - 1);
  }
</script>

<svelte:window onkeydown={key} />

{#snippet says(line: string)}
  <div class="flex items-start gap-2">
    {#if !wide}<div class="shrink-0 -ml-1"><Rook mood="smug" size={52} /></div>{/if}
    <p class="text-base lg:text-lg leading-snug pt-1 lg:pt-0">{line}</p>
  </div>
{/snippet}

{#snippet tile(c: Card, sub?: string, status?: "evolution" | "hero" | "both" | null)}
  {#if onCard}
    <button class="block w-full cursor-pointer transition active:scale-95" aria-label={`More about ${c.name}`} onclick={() => onCard(c)}><CardTile card={c} {sub} {status} /></button>
  {:else}
    <CardTile card={c} {sub} {status} />
  {/if}
{/snippet}

{#snippet slide(id: string)}
          {#if id === "you"}
            <div class="text-center">
              <div class="flex justify-center"><Rook mood="happy" size={wide ? 80 : 120} /></div>
              <h2 class="text-3xl mt-1">{o.player.name}</h2>
              <p class="text-sm text-base-content/60">{o.player.clan ?? "No clan"}</p>
            </div>
            <p class="text-lg leading-snug text-center">{report.intro}</p>
            <div class="grid grid-cols-3 divide-x divide-base-content/10 text-center">
              <div><Icon name="trophy" class="w-5 h-5 mx-auto mb-1 text-primary" /><div class="stat-num text-2xl text-primary">{o.player.trophies?.toLocaleString() ?? "—"}</div><div class="text-xs text-base-content/60 mt-1">Trophies</div></div>
              <div><Icon name="crown" class="w-5 h-5 mx-auto mb-1 text-accent" /><div class="stat-num text-2xl">{Math.round(o.record.win_rate * 100)}%</div><div class="text-xs text-base-content/60 mt-1">Won {o.record.wins} of {o.record.games}</div></div>
              <div><Icon name="trend" class="w-5 h-5 mx-auto mb-1 {o.net_trophies >= 0 ? 'text-success' : 'text-error'}" /><div class="stat-num text-2xl" class:text-success={o.net_trophies > 0} class:text-error={o.net_trophies < 0}>{o.net_trophies > 0 ? "+" : ""}{o.net_trophies}</div><div class="text-xs text-base-content/60 mt-1">Trophies gained</div></div>
            </div>
            {#if thin}
              <p class="text-sm text-center text-base-content/70" data-testid="thin">{o.record.games ? `I've only got ${o.record.games} ${o.record.games === 1 ? "game" : "games"} to go on, so some cards stay hidden until you play more.` : "Play a few games and come back. I'll have plenty to say."}</p>
            {/if}
            {#if report.similar?.enough}
              <p class="text-xs text-center text-base-content/60" data-testid="similar">Compared with {report.similar.battles.toLocaleString()} similar battles from {report.similar.players.toLocaleString()} players{report.similar.near ? " near your trophies" : ""}, last {report.similar.days} days.</p>
            {/if}
          {:else if id === "wow"}
            <h2 class="text-2xl lg:text-3xl flex items-center gap-2"><Icon name="sparkle" class="w-6 h-6 text-primary" />Things you didn't know</h2>
            <ul class="grid gap-3" data-testid="surprises">
              {#each s.surprises as x (x.kind)}
                <li class="panel p-3 flex items-center gap-3.5" data-kind={x.kind}>
                  <div class="w-16 lg:w-20 shrink-0 relative">
                    {#if x.kind === "signature_card"}
                      {@render tile(x.card)}
                      <span class="absolute -top-1.5 -right-1.5 rounded-full bg-primary text-primary-content p-1"><Icon name="star" class="w-3.5 h-3.5" /></span>
                    {:else if x.kind === "free_wins"}
                      {@render tile(x.cards[0])}
                      <span class="absolute -bottom-1.5 inset-x-0 mx-auto w-fit rounded-full bg-success text-success-content text-xs font-bold px-1.5 stat-num">{x.cards[0].wins}–{x.cards[0].games - x.cards[0].wins}</span>
                    {:else if x.kind === "unique_deck"}
                      <div class="grid grid-cols-2 gap-0.5">{#each x.deck as c (c.id)}<CardTile card={c} />{/each}</div>
                    {:else if x.kind === "evolution_gap"}
                      {#if evolved.length}
                        <div class="grid grid-cols-2 gap-0.5">{#each evolved.slice(0, 4) as c (c.id)}<CardTile card={c} />{/each}</div>
                      {:else}
                        <div class="aspect-square rounded-xl bg-accent/15 text-accent grid place-items-center"><Icon name="bolt" class="w-8 h-8" /></div>
                      {/if}
                    {/if}
                  </div>
                  <div class="flex-1 min-w-0">
                    <div class="font-semibold leading-tight">{x.headline}</div>
                    {#if x.kind === "signature_card" || x.kind === "evolution_gap"}
                      {@const bars = x.kind === "signature_card"
                        ? [{ label: "You", v: x.record.win_rate, tone: "bg-primary" }, { label: "Others", v: x.field_win_rate, tone: "bg-base-content/30" }]
                        : [{ label: `< ${x.your_evolutions} evos`, v: x.fewer.win_rate, tone: "bg-success" }, { label: `${x.your_evolutions} evos`, v: x.matched.win_rate, tone: "bg-error" }]}
                      <div class="mt-2 space-y-1">
                        {#each bars as b (b.label)}
                          <div class="flex items-center gap-2 text-xs">
                            <span class="w-14 shrink-0 text-base-content/70">{b.label}</span>
                            <div class="flex-1 h-2 rounded-full bg-base-content/10 overflow-hidden"><div class="h-full rounded-full {b.tone}" style="width: {b.v * 100}%"></div></div>
                            <span class="w-9 text-right stat-num">{pct(b.v)}</span>
                          </div>
                        {/each}
                      </div>
                      <p class="text-xs text-base-content/60 mt-1.5">{x.kind === "signature_card" ? `${x.record.games} of your games vs ${x.field_games.toLocaleString()} at your trophies` : `Your win rate by how many evolved cards they bring. You bring ${x.your_evolutions}.`}</p>
                    {:else if x.kind === "unique_deck"}
                      <div class="mt-1 flex items-baseline gap-1.5"><span class="stat-num text-2xl text-primary">{x.players_alike.toLocaleString()}</span><span class="text-sm text-base-content/70">of {x.players_total.toLocaleString()} players</span></div>
                      <p class="text-xs text-base-content/60">at your trophies ran {x.shared_cards} of your 8 cards this week</p>
                    {:else}
                      <p class="text-sm text-base-content/70 mt-1">{x.detail}</p>
                      {#if x.cards.length > 1}
                        <div class="flex gap-3 mt-2">
                          {#each x.cards.slice(1) as c (c.id)}
                            <div class="flex items-center gap-1.5"><div class="w-7">{@render tile(c)}</div><span class="text-xs stat-num text-success">{c.wins}–{c.games - c.wins}</span></div>
                          {/each}
                        </div>
                      {/if}
                    {/if}
                  </div>
                </li>
              {/each}
            </ul>
          {:else if id === "deck"}
            <h2 class="text-2xl lg:text-3xl">Your deck</h2>
            <div class="grid grid-cols-4 gap-x-3 gap-y-4">
              {#each o.deck as c (c.id)}{@render tile(c, c.name, c.status)}{/each}
            </div>
            <p class="text-sm text-base-content/70">{onCard ? `${wide ? "Click" : "Tap"} any card to find out more.` : `${o.avg_elixir} average elixir.`}</p>
          {:else if id === "trend" && s.trend}
            <h2 class="text-2xl lg:text-3xl">Your form</h2>
            {@render says(s.lines.trend ?? "")}
            <Visual visual={s.trend} {onCard} />
          {:else if id === "close" && s.close}
            <h2 class="text-2xl lg:text-3xl">Close games</h2>
            {@render says(s.lines.close ?? "")}
            <Visual visual={s.close} />
          {:else if id === "worst" && s.worst}
            <h2 class="text-2xl lg:text-3xl">What beats you</h2>
            {@render says(s.lines.worst ?? "")}
            <Visual visual={s.worst} {onCard} titled={false} />
          {:else if id === "elixir" && s.elixir}
            <h2 class="text-2xl lg:text-3xl">What your deck costs</h2>
            {@render says(s.lines.elixir ?? "")}
            <Visual visual={s.elixir} />
          {:else if id === "try"}
            {@const weak = s.replace.weakest}
            {@const swap = s.replace.candidates[0]}
            <h2 class="text-2xl lg:text-3xl">{swap || weak?.real ? "One thing to try" : "Anything to change?"}</h2>
            {@render says(report.story.lines.replace)}
            {#if weak}
              <div class="rounded-xl bg-base-200 border border-base-content/10 p-4 grid {swap ? 'grid-cols-[1fr_auto_1fr]' : 'grid-cols-1'} items-center gap-3">
                <div class="w-20 mx-auto">{@render tile(weak, weak.name)}</div>
                {#if swap}
                  <Icon name="swap" class="w-6 h-6 text-primary" />
                  <div class="w-20 mx-auto">{@render tile(swap, swap.name)}</div>
                  <div class="col-span-3 text-center text-sm"><span class="stat-num text-success">+{(swap.gain * 100).toFixed(1)}</span> <span class="text-base-content/70">wins per 100 games in decks like yours</span></div>
                {/if}
              </div>
            {/if}
          {:else}
            <div class="text-center lg:text-left">
              {#if !wide}<div class="flex justify-center"><Rook mood="idle" size={96} /></div>{/if}
              <h2 class="text-2xl lg:text-3xl mt-1 lg:mt-0">What do you want?</h2>
            </div>
            <div class="grid gap-2.5">
              {#each goals as g (g.id)}
                {@const art = goalArt(g.id)}
                <button class="panel px-4 py-3 text-left cursor-pointer flex items-center gap-3 hover:border-primary/60" onclick={() => onGoal(g.label, g.id)}>
                  <span class="w-10 h-10 shrink-0 rounded-xl bg-primary/15 text-primary grid place-items-center"><Icon name={g.icon} class="w-5 h-5" /></span>
                  <span class="flex-1 min-w-0"><span class="block text-base font-semibold">{g.title}</span><span class="block text-sm text-base-content/70">{g.detail}</span></span>
                  {#if art}<span class="w-8 shrink-0"><CardTile card={art} /></span>{/if}
                  <span class="text-primary text-lg" aria-hidden="true">→</span>
                </button>
              {/each}
            </div>
            {#if report.ai_enabled}
              <button class="btn btn-ghost font-medium cursor-pointer" onclick={onAsk}>Ask something else</button>
            {/if}
            <p class="text-[11px] text-base-content/40 text-center">
              This material is unofficial and is not endorsed by Supercell. For more information see
              <a class="underline" href="https://www.supercell.com/fan-content-policy" target="_blank" rel="noreferrer">Supercell's Fan Content Policy</a>.
            </p>
          {/if}
{/snippet}

{#if wide}
  <div class="fixed inset-0 z-20 bg-base-100 overflow-y-auto" in:fly={{ y: 700, duration: 420, easing: cubicOut }} data-testid="dashboard">
    <header class="sticky top-0 z-10 bg-base-100/90 backdrop-blur border-b border-base-content/10">
      <div class="mx-auto max-w-6xl px-8 h-16 flex items-center justify-between gap-6">
        <div class="flex items-center gap-2"><Rook mood="smug" size={40} /><span class="text-xl font-semibold">Rook</span></div>
        <nav class="flex items-center gap-1" aria-label="Sections" data-testid="section-nav">
          {#each sections as group, i (group[0])}
            <button class="btn btn-ghost btn-sm font-medium cursor-pointer" onclick={() => document.getElementById(`section-${i}`)?.scrollIntoView({ behavior: "smooth", block: "start" })}>{sectionNames[group[0]]}</button>
          {/each}
        </nav>
        <button class="btn btn-ghost btn-sm font-medium cursor-pointer text-base-content/70" onclick={onReset}>Not you?</button>
      </div>
    </header>
    <div class="mx-auto max-w-6xl px-8">
      {#each sections as group, i (group[0])}
        <section id={`section-${i}`} class="scroll-mt-16 py-12 grid grid-cols-2 gap-16 items-start {i ? 'border-t border-base-content/10' : ''}" data-section>
          {#each group as id (id)}
            <div class="flex flex-col gap-4" data-card={id}>{@render slide(id)}</div>
          {/each}
        </section>
      {/each}
    </div>
  </div>
{:else}
<div class="fixed inset-0 z-20 bg-base-100 flex flex-col" in:fly={{ y: 700, duration: 420, easing: cubicOut }} data-testid="story">
  <div class="mx-auto w-full max-w-md flex flex-col flex-1 min-h-0 px-5 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))]">
    <div class="flex gap-1" role="progressbar" aria-label="Your story" aria-valuemin="1" aria-valuemax={slides.length} aria-valuenow={index + 1}>
      {#each slides as id, i (id)}
        <span class="flex-1 h-1 rounded-full transition-colors {i <= index ? 'bg-primary' : 'bg-base-content/15'}"></span>
      {/each}
    </div>
    <div class="flex justify-between items-center mt-2 h-6">
      <button class="text-xs text-base-content/60 cursor-pointer disabled:invisible" disabled={index === 0} onclick={() => go(0)} data-testid="start-over">↺ Start over</button>
      <button class="text-xs text-base-content/60 cursor-pointer" onclick={onReset} data-testid="not-you">Not you?</button>
    </div>

    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="flex-1 min-h-0 overflow-y-auto mt-1 touch-pan-y select-none" onpointerdown={pointerDown} onpointerup={pointerUp} data-testid="story-slide" data-slide={current}>
      {#key current}
        <div class="min-h-full flex flex-col justify-center gap-5 py-2" in:fly={{ x: 50 * direction, duration: 220, easing: cubicOut }}>
          {@render slide(current)}
        </div>
      {/key}
    </div>

    {#if !last}
      <div class="grid grid-cols-[auto_1fr] gap-2 mt-3">
        <button class="btn btn-ghost cursor-pointer disabled:opacity-0" disabled={index === 0} aria-label="Back" onclick={() => go(index - 1)}>←</button>
        <button class="btn btn-primary cursor-pointer font-semibold" onclick={() => go(index + 1)} data-testid="story-next">Next</button>
      </div>
    {/if}
  </div>
</div>
{/if}
