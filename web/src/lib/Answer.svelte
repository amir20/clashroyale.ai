<script lang="ts">
  import CardTile from "./CardTile.svelte";
  import Visual from "./Visual.svelte";
  let { id, data }: { id: string; data: any } = $props();
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const per100 = (x: number) => `${x > 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)} per 100`;
  const verdict = (c: any) =>
    !c.real
      ? { label: "No difference", tone: "badge-ghost" }
      : c.gain > 0
        ? { label: "Helps", tone: "badge-success" }
        : { label: "Hurts", tone: "badge-error" };
  const versus = (c: any) =>
    !c.real ? "Too few games to tell" : c.diff > 0 ? "Better than most" : "Worse than most";
  // What each check in "Why do I keep losing?" looked at, in the player's words.
  const checks: Record<string, string> = {
    worst_matchups: "A card that beats you",
    card_levels: "Card levels",
    after_two_losses: "Playing on after two losses",
    close_games: "One-crown games",
    three_crowned: "Getting three-crowned",
    deck_switching: "Switching decks",
  };
  const detail = (k: string, f: any): string => {
    switch (k) {
      case "worst_matchups":
        return f.cards.length ? f.cards.map((c: any) => `${c.name} ${c.wins}–${c.games - c.wins}`).join(" · ") : "No card beats you more than chance";
      case "card_levels":
        return `Your deck ${f.your_deck_level} vs opponents ${f.typical_opponent_level}` + (f.in_those_games.games ? ` · ${pct(f.in_those_games.win_rate)} when outleveled` : "");
      case "after_two_losses":
        return `${pct(f.in_those_games.win_rate)} next game vs ${pct(f.other_games.win_rate)} otherwise · ${f.in_those_games.games} games`;
      case "close_games":
        return `Won ${f.wins} of ${f.games}`;
      case "three_crowned":
        return `${f.three_crowned_losses} of ${f.losses} losses`;
      case "deck_switching":
        return `${f.decks_used} decks in your last ${f.recent_games} games`;
    }
    return "";
  };
  const sureness = (games: number) => (games < 30 ? `${games} games, too early to be sure` : `${games} games`);
</script>

{#if id === "why_losing" && data.enough}
  <ul class="space-y-2.5" data-testid="game-plan">
    {#each Object.keys(checks) as k (k)}
      {@const f = data.findings[k]}
      <li class="flex items-start gap-3">
        <span class="badge badge-sm badge-soft whitespace-nowrap mt-0.5 w-24 justify-center shrink-0 {f.real ? 'badge-error' : 'badge-success'}">{f.real ? "Costing you" : "Fine"}</span>
        <div class="flex-1 min-w-0">
          <div class="text-sm">{checks[k]}</div>
          <div class="text-xs opacity-70 tabular-nums">{detail(k, f)}</div>
        </div>
      </li>
    {/each}
  </ul>
{:else if id === "replace_card" && data.enough}
  <p class="text-sm opacity-80 mb-3">
    How {data.family_decks.toLocaleString()} decks like yours do with and without each card, from
    {data.family_players.toLocaleString()} players this week.
  </p>
  <ul class="space-y-3">
    {#each data.yours as c (c.id)}
      {@const v = verdict(c)}
      <li class="flex items-center gap-3">
        <div class="w-10 shrink-0"><CardTile card={c} /></div>
        <div class="flex-1 min-w-0">
          <div class="truncate">{c.name}</div>
          <div class="text-xs opacity-70 tabular-nums whitespace-nowrap">{per100(c.gain)} games won vs decks without it</div>
        </div>
        <span class="badge badge-sm badge-soft whitespace-nowrap {v.tone}">{v.label}</span>
      </li>
    {/each}
  </ul>
  {#if data.candidates.length}
    <h4 class="text-sm font-semibold mt-5 mb-2">Worth a try</h4>
    <div class="grid grid-cols-4 gap-2 max-w-md">
      {#each data.candidates as c (c.id)}<CardTile card={c} sub={`${per100(c.gain)} · ${c.players} players`} />{/each}
    </div>
  {/if}
{:else if id === "best_cards"}
  <ul class="space-y-3">
    {#each data.cards as c (c.id)}
      <li class="flex items-center gap-3">
        <div class="w-10 shrink-0"><CardTile card={c} /></div>
        <div class="flex-1 min-w-0">
          <div class="flex justify-between text-sm gap-2"><span class="truncate">{c.name}</span><span class="tabular-nums whitespace-nowrap">you {pct(c.your_win_rate)} <span class="opacity-60">· everyone {pct(c.field_win_rate)}</span></span></div>
          <progress class="progress progress-primary h-2 w-full" value={c.your_win_rate * 100} max="100"></progress>
          <div class="text-xs opacity-70">{c.games} games · {versus(c)}</div>
        </div>
      </li>
    {/each}
  </ul>
{:else if id === "nemesis"}
  <h4 class="text-sm font-semibold mb-2 text-error">Your problem cards</h4>
  <div class="grid grid-cols-5 gap-2 max-w-lg">
    {#each data.worst as c (c.id)}<CardTile card={c} sub={`won ${c.wins} of ${c.games}`} />{/each}
  </div>
  {#if data.best.length}
    <h4 class="text-sm font-semibold mt-5 mb-2 text-success">Free wins</h4>
    <div class="grid grid-cols-5 gap-2 max-w-lg">
      {#each data.best as c (c.id)}<CardTile card={c} sub={`won ${c.wins} of ${c.games}`} />{/each}
    </div>
  {/if}
{:else if id === "best_deck"}
  <ul class="space-y-4">
    {#each data.decks as d, i (i)}
      <li>
        <div class="flex justify-between text-sm mb-1 gap-2">
          <span>{d.wins}–{d.losses} · won {pct(d.win_rate)} {#if d.current}<span class="badge badge-accent badge-xs ml-1">playing now</span>{/if}</span>
          <span class="tabular-nums opacity-70 whitespace-nowrap">{d.net_trophies > 0 ? "+" : ""}{d.net_trophies} 🏆</span>
        </div>
        <div class="grid grid-cols-8 gap-1 max-w-xl">{#each d.cards as c (c.id)}<CardTile card={c} />{/each}</div>
        <div class="text-xs opacity-70 mt-1">{sureness(d.games)}</div>
      </li>
    {/each}
  </ul>
  {#if data.proven}
    <h4 class="text-sm font-semibold mt-5 mb-2">A proven deck with your {data.proven.kept.join(" and ")}</h4>
    <Visual visual={data.proven} titled={false} />
  {/if}
{/if}
