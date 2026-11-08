<script lang="ts">
  import CardTile from "./CardTile.svelte";

  // Drawn from figures the server computed; the model only chooses which visual to show.
  let { visual: v }: { visual: any } = $props();
  const pct = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const verdicts: Record<string, { label: string; tone: string }> = {
    better: { label: "Looks like an upgrade", tone: "text-success" },
    worse: { label: "Looks like a downgrade", tone: "text-error" },
    even: { label: "No clear difference", tone: "text-base-content/70" },
    unknown: { label: "Not enough games to compare", tone: "text-base-content/70" },
  };
</script>

<div class="rounded-xl bg-base-100 border border-base-content/10 p-4" data-testid={`visual-${v.type}`}>
  {#if v.type === "swap"}
    {@const verdict = verdicts[v.verdict]}
    <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-3 max-w-sm mx-auto">
      <div class="text-center">
        <div class="max-w-20 mx-auto"><CardTile card={v.out} /></div>
        <div class="stat-num text-xl mt-2">{pct(v.out.win_rate)}</div>
        <div class="text-xs text-base-content/60">{v.out.name}</div>
        <div class="text-[11px] text-base-content/50">{v.out.games.toLocaleString()} games</div>
      </div>
      <span class="text-primary text-xl" aria-hidden="true">→</span>
      <div class="text-center">
        <div class="max-w-20 mx-auto"><CardTile card={v.in} /></div>
        <div class="stat-num text-xl mt-2 text-primary">{pct(v.in.win_rate)}</div>
        <div class="text-xs text-base-content/60">{v.in.name}</div>
        <div class="text-[11px] text-base-content/50">{v.in.games.toLocaleString()} games</div>
      </div>
    </div>
    <p class="text-center text-sm font-semibold mt-3 {verdict.tone}">{verdict.label}</p>
    <p class="text-center text-xs text-base-content/60 mt-0.5">Win rate of decks like yours running each card</p>
  {:else if v.type === "matchup"}
    <div class="flex items-center gap-4">
      <div class="w-16 shrink-0"><CardTile card={v.card} /></div>
      <div class="flex-1 min-w-0">
        <div class="text-xs text-base-content/60">Your record against {v.card.name}</div>
        {#if v.games}
          <div class="stat-num text-2xl mt-0.5">Won {v.wins} of {v.games}</div>
          <div class="flex h-2 gap-0.5 mt-2 rounded-full overflow-hidden" aria-hidden="true">
            <div class="bg-success" style="width: {(v.wins / v.games) * 100}%"></div>
            <div class="flex-1 bg-error"></div>
          </div>
          <div class="text-xs text-base-content/60 mt-1.5">
            {pct(v.win_rate)} against it · {pct(v.overall_win_rate)} overall{v.real ? "" : " · too few games to be sure"}
          </div>
        {:else}
          <div class="text-sm mt-1">You haven't faced it yet.</div>
        {/if}
      </div>
    </div>
  {:else if v.type === "ranking"}
    <h4 class="text-sm font-semibold mb-3">{v.kind === "counters" ? `What beats ${v.card.name}` : `Best with ${v.card.name}`}</h4>
    {#if v.rows.length}
      <ul class="space-y-2.5">
        {#each v.rows as c, i (c.id)}
          <li class="flex items-center gap-3">
            <span class="w-4 text-xs text-base-content/50 tabular-nums">{i + 1}</span>
            <div class="w-9 shrink-0"><CardTile card={c} /></div>
            <span class="flex-1 min-w-0 truncate text-sm">{c.name}</span>
            <span class="text-right">
              <span class="stat-num text-sm">{pct(c.win_rate)}</span>
              <span class="block text-[11px] text-base-content/50">{c.games.toLocaleString()} games</span>
            </span>
          </li>
        {/each}
      </ul>
      {#if v.kind === "counters"}<p class="text-xs text-base-content/60 mt-3">How often decks with {v.card.name} win against each card</p>{/if}
    {:else}
      <p class="text-sm text-base-content/70">Not enough games yet.</p>
    {/if}
  {:else if v.type === "deck"}
    <div class="grid grid-cols-4 gap-2 max-w-xs mx-auto">
      {#each v.cards as c (c.id)}<CardTile card={c} />{/each}
    </div>
    <p class="text-center text-sm mt-3">
      <span class="stat-num">{pct(v.win_rate)}</span> wins · {v.games.toLocaleString()} games · {v.avg_elixir} elixir
    </p>
    <a class="btn btn-primary btn-sm w-full max-w-xs mx-auto flex mt-3 cursor-pointer" href={v.copy_url} target="_blank" rel="noreferrer">Copy to game</a>
  {/if}
</div>
