<script lang="ts">
  import CardTile from "./CardTile.svelte";
  import Icon from "./Icon.svelte";

  // Drawn from figures the server computed; the model only chooses which visual to show.
  // `them` words it about someone else, such as an opponent, instead of the viewer.
  let { visual: v, onCard, titled = true, them = false }: { visual: any; onCard?: (card: any) => void; titled?: boolean; them?: boolean } = $props();
  const your = $derived(them ? "their" : "your");
  const Your = $derived(them ? "Their" : "Your");
  const you = $derived(them ? "they" : "you");
  const pct = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const pct1 = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
  const gain = (x: number) => `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}`;
  const gainTone = (x: number) => (x > 0 ? "text-success" : x < 0 ? "text-error" : "text-base-content/60");
  const dayLabel = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  // Global numbers come from players at the viewer's trophies when there were enough of them.
  const nearYou = (scope: string | undefined) => !!scope?.startsWith("Other players");
  const names = (cards: any[]) => cards.map((c) => c.name).join(" and ");
  const trends: Record<string, { label: string; tone: string }> = {
    improving: { label: "Trending up", tone: "text-success" },
    slipping: { label: "Trending down", tone: "text-error" },
    steady: { label: "Too close to call", tone: "text-base-content/70" },
  };
  const verdicts: Record<string, { label: string; tone: string }> = {
    better: { label: "Looks like an upgrade", tone: "text-success" },
    worse: { label: "Looks like a downgrade", tone: "text-error" },
    even: { label: "No clear difference", tone: "text-base-content/70" },
    unknown: { label: "Not enough games to compare", tone: "text-base-content/70" },
  };
</script>

{#snippet tile(c: any, dim = false)}
  {#if onCard}
    <button class="block w-full cursor-pointer transition active:scale-95" aria-label={`More about ${c.name}`} onclick={() => onCard(c)}><CardTile card={c} {dim} /></button>
  {:else}
    <CardTile card={c} {dim} />
  {/if}
{/snippet}

{#snippet legend()}
  <p class="flex items-center gap-3 text-xs text-base-content/60" data-testid="compare-legend">
    <span class="flex items-center gap-1"><span class="inline-block w-2.5 h-3 rounded-sm bg-base-content/25"></span>In {your} deck now</span>
    <span class="flex items-center gap-1"><span class="inline-block w-2.5 h-3 rounded-sm bg-primary"></span>Different</span>
  </p>
{/snippet}

<div class="rounded-xl bg-base-100 border border-base-content/10 p-4" data-testid={`visual-${v.type}`}>
  {#if v.type === "swap"}
    {@const verdict = verdicts[v.verdict]}
    <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-3 max-w-sm mx-auto">
      <div class="text-center">
        <div class="max-w-20 mx-auto">{@render tile(v.out)}</div>
        <div class="text-sm font-medium mt-2">{v.out.name}</div>
        <div class="text-[11px] text-base-content/50">{v.out.games.toLocaleString()} games</div>
      </div>
      <span class="text-primary text-xl" aria-hidden="true">→</span>
      <div class="text-center">
        <div class="max-w-20 mx-auto">{@render tile(v.in)}</div>
        <div class="text-sm font-medium mt-2 text-primary">{v.in.name}</div>
        <div class="text-[11px] text-base-content/50">{v.in.games.toLocaleString()} games</div>
      </div>
    </div>
    <p class="text-center text-sm font-semibold mt-3 {verdict.tone}">{verdict.label}</p>
    {#if v.gap != null}
      <p class="text-center text-sm mt-1" data-testid="swap-gap">{v.in.name} decks win about <span class="stat-num">{Math.abs(v.gap * 100).toFixed(1)} {v.gap >= 0 ? "more" : "fewer"}</span> games per 100</p>
    {/if}
    <p class="text-center text-xs text-base-content/60 mt-0.5" data-testid="swap-match">{v.match?.core ? `Decks built on ${your} core: ${v.match.core.map((c: any) => c.name).join(", ")}` : v.match?.exact ? `${Your} exact deck with only this card changed` : `Decks sharing at least ${v.min_shared_cards} of ${your} other cards`}{v.near ? ", near " + your + " trophies" : ""}</p>
    {#if v.match?.also}
      <p class="text-center text-xs mt-2" data-testid="swap-also">{Math.round(v.match.also.share * 100)}% of these decks also swapped {v.match.also.dropped.map((c: any) => c.name).join(" + ")} for {v.match.also.added.map((c: any) => c.name).join(" + ")}</p>
    {/if}
    {#if v.watch_out?.length}
      <p class="text-center text-xs mt-1 text-warning" data-testid="swap-watch">Weaker against {v.watch_out.map((c: any) => `${c.name} (${(c.gap * 100).toFixed(0)} per 100)`).join(", ")}</p>
    {/if}
  {:else if v.type === "matchup"}
    <div class="flex items-center gap-4">
      <div class="w-16 shrink-0">{@render tile(v.card)}</div>
      <div class="flex-1 min-w-0">
        <div class="text-xs text-base-content/60">Your record against {v.card.name}</div>
        {#if v.games}
          <div class="stat-num text-2xl mt-0.5">Won {v.wins} of {v.games}</div>
          <div class="flex h-2 gap-0.5 mt-2 rounded-full overflow-hidden" aria-hidden="true">
            {#if v.wins}<div class="bg-success" style="width: {(v.wins / v.games) * 100}%"></div>{/if}
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
    <h4 class="text-sm font-semibold mb-3">{v.kind === "top" ? "Best cards right now" : v.kind === "counters" ? `What beats ${v.card.name}` : `Best with ${v.card.name}`}</h4>
    {#if v.rows.length}
      <ul class="space-y-2.5">
        {#each v.rows as c, i (c.id)}
          <li class="flex items-center gap-3">
            <span class="w-4 text-xs text-base-content/50 tabular-nums">{i + 1}</span>
            <div class="w-9 shrink-0">{@render tile(c)}</div>
            <span class="flex-1 min-w-0 truncate text-sm">{c.name}</span>
            <span class="text-right">
              <span class="stat-num text-sm">{pct1(c.win_rate)} <span class="text-xs {gainTone(c.gain)}">{gain(c.gain)}</span></span>
              <span class="block text-[11px] text-base-content/50">{c.games.toLocaleString()} games</span>
            </span>
          </li>
        {/each}
      </ul>
      <p class="text-xs text-base-content/60 mt-3">
        {#if v.kind === "top"}{nearYou(v.scope) ? "Players at your trophies" : "Across all players"}, compared with a 50% coin flip{:else if v.kind === "counters"}How often {v.card.name} decks win against each card{nearYou(v.scope) ? " at your trophies" : ""}. They usually win {pct1(v.baseline)}.{:else}{v.card.name} decks usually win {pct1(v.baseline)}{nearYou(v.scope) ? " at your trophies" : ""}. Plus or minus is the change with each partner.{/if}
      </p>
    {:else}
      <p class="text-sm text-base-content/70">Not enough games yet.</p>
    {/if}
  {:else if v.type === "deck"}
    <div class="grid grid-cols-4 gap-2 max-w-xs mx-auto">
      {#each v.cards as c (c.id)}{@render tile(c, !!c.yours)}{/each}
    </div>
    {#if v.cards.some((c: any) => c.yours)}<div class="flex justify-center mt-2">{@render legend()}</div>{/if}
    <p class="text-center text-sm mt-3">
      <span class="stat-num">{pct(v.win_rate)}</span> wins · {v.games.toLocaleString()} games · {v.players.toLocaleString()} players · {v.avg_elixir} elixir
    </p>
    <p class="text-xs text-center text-base-content/60 mt-0.5">{nearYou(v.scope) ? "Proven by players at your trophies this week" : "Proven across all players this week"}</p>
    <a class="btn btn-primary btn-sm w-full max-w-xs mx-auto flex mt-3 cursor-pointer" href={v.copy_url} target="_blank" rel="noreferrer">Copy to game</a>
  {:else if v.type === "worst_matchups"}
    {#if titled}<h4 class="text-sm font-semibold mb-3">What beats {them ? "them" : "you"} most</h4>{/if}
    <ul class="space-y-3">
      {#each v.rows as c (c.id)}
        <li class="flex items-center gap-3" class:opacity-55={!c.real}>
          <div class="w-9 shrink-0">{@render tile(c)}</div>
          <div class="flex-1 min-w-0">
            <div class="flex items-baseline justify-between gap-2 text-sm">
              <span class="truncate">{c.name}</span>
              <span class="stat-num shrink-0">Won {c.wins} of {c.games}</span>
            </div>
            <div class="flex h-1.5 gap-0.5 mt-1.5 rounded-full overflow-hidden" aria-hidden="true">
              {#if c.wins}<div class="bg-success" style="width: {(c.wins / c.games) * 100}%"></div>{/if}
              <div class="flex-1 bg-error"></div>
            </div>
          </div>
        </li>
      {/each}
    </ul>
    <p class="text-xs text-base-content/60 mt-3">Games where {your} opponent had each card. {them ? "They win" : "You win"} {pct(v.overall_win_rate)} overall.{v.rows.some((c: any) => !c.real) ? " Faded cards: too few games to be sure." : ""}</p>
  {:else if v.type === "trend"}
    {#if v.verdict === "changed_decks"}
      <h4 class="text-sm font-semibold">Decks {you}'ve played, oldest first</h4>
      <div class="mt-1 mb-3">{@render legend()}</div>
      <ul class="space-y-3 lg:space-y-6">
        {#each v.decks as d, i (i)}
          <li>
            <div class="flex items-center justify-between gap-3 text-sm mb-1.5">
              {#if d.current}
                <span class="font-semibold text-primary">{Your} deck now</span>
              {:else}
                <span class="text-base-content/70">{d.added.length} {d.added.length === 1 ? "card" : "cards"} different</span>
              {/if}
              <span class="stat-num shrink-0">{d.wins}–{d.losses}</span>
            </div>
            <div class="grid grid-cols-8 gap-1" data-testid="deck-compare" aria-label={d.current ? `${Your} deck now` : `${names(d.added)} instead of ${names(d.removed)}`}>
              {#each d.cards as c (c.id)}{@render tile(c, !d.current && c.yours)}{/each}
            </div>
            {#if !d.current && d.added.length <= 2}
              <p class="text-sm mt-1.5" data-testid="deck-change-text">{names(d.added)} instead of {names(d.removed)}</p>
            {/if}
            <div class="flex h-1.5 lg:h-2 gap-0.5 mt-1.5 lg:mt-2 rounded-full overflow-hidden" aria-hidden="true">
              {#if d.wins}<div class="bg-success" style="width: {(d.wins / d.games) * 100}%"></div>{/if}
              <div class="flex-1 bg-error"></div>
            </div>
          </li>
        {/each}
      </ul>
    {:else}
      {@const verdict = trends[v.verdict]}
      {@const most = Math.max(...v.days.map((d: any) => d.games))}
      <div class="text-xs text-center text-base-content/60 mb-2">Same deck, older vs newer</div>
      <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-3 max-w-sm mx-auto text-center">
        <div>
          <div class="stat-num text-2xl">{pct(v.older.win_rate)}</div>
          <div class="text-xs text-base-content/60">Older {v.older.games} games</div>
        </div>
        <span class="text-primary text-xl" aria-hidden="true">→</span>
        <div>
          <div class="stat-num text-2xl text-primary">{pct(v.newer.win_rate)}</div>
          <div class="text-xs text-base-content/60">Newer {v.newer.games} games</div>
        </div>
      </div>
      <p class="text-center text-sm font-semibold mt-3 {verdict.tone}">{verdict.label}</p>
      <div class="flex items-end gap-1.5 h-20 mt-4" role="img" aria-label="Wins and losses by day">
        {#each v.days as d (d.day)}
          <div class="flex-1 min-w-0 h-full flex flex-col justify-end gap-px">
            <div class="bg-error rounded-t-sm" style="height: {((d.games - d.wins) / most) * 100}%"></div>
            <div class="bg-success" style="height: {(d.wins / most) * 100}%"></div>
          </div>
        {/each}
      </div>
      <div class="flex gap-1.5 mt-1">
        {#each v.days as d (d.day)}
          <div class="flex-1 min-w-0 text-center text-[10px] leading-tight text-base-content/60">{dayLabel(d.day)}<br />{d.wins}–{d.games - d.wins}</div>
        {/each}
      </div>
    {/if}
    {#if v.lobbies}
      <p class="text-xs text-base-content/60 mt-3">Typical opponent: {v.lobbies.older.toLocaleString()} → {v.lobbies.newer.toLocaleString()} trophies{v.lobbies.harder ? ", so they got tougher." : ", no tougher."}</p>
    {/if}
  {:else if v.type === "close_games"}
    {@const c = v.close_games}
    <div class="text-xs text-base-content/60 flex items-center gap-1"><Icon name="crown" class="w-3.5 h-3.5 text-primary" />{Your} one-crown games</div>
    <div class="stat-num text-2xl mt-0.5">Won {c.wins} of {c.games}</div>
    <div class="flex h-2 gap-0.5 mt-2 rounded-full overflow-hidden" aria-hidden="true">
      {#if c.wins}<div class="bg-success" style="width: {(c.wins / c.games) * 100}%"></div>{/if}
      <div class="flex-1 bg-error"></div>
    </div>
    <div class="grid grid-cols-2 gap-3 mt-4 text-center">
      <div>
        <div class="stat-num text-lg flex items-center justify-center gap-1"><Icon name="elixir" class="w-4 h-4 text-fuchsia-400" />{v.elixir_leaked_close_wins?.toFixed(1) ?? "—"}</div>
        <div class="text-xs text-base-content/60">Elixir wasted when {you} win them</div>
      </div>
      <div>
        <div class="stat-num text-lg flex items-center justify-center gap-1"><Icon name="elixir" class="w-4 h-4 text-fuchsia-400" />{v.elixir_leaked_close_losses?.toFixed(1) ?? "—"}</div>
        <div class="text-xs text-base-content/60">Elixir wasted when {you} lose them</div>
      </div>
    </div>
    <p class="text-xs text-base-content/60 mt-3">Typical game. For comparison, {v.elixir_leaked_three_crowned?.toFixed(1) ?? "—"} when {you} get three-crowned.</p>
  {:else if v.type === "elixir"}
    <div class="stat-num text-2xl flex items-center gap-1.5"><Icon name="elixir" class="w-6 h-6 text-fuchsia-400" />{v.avg_elixir} elixir</div>
    <div class="text-xs text-base-content/60 mt-0.5">
      {v.higher_than >= 0.5 ? `Costs more than ${pct(v.higher_than)} of decks` : `Cheaper than ${pct(1 - v.higher_than)} of decks`} · the average deck is {v.average_deck}
    </div>
    <ul class="space-y-2 mt-4">
      {#each v.bands as b (b.start)}
        <li class="flex items-center gap-2 text-xs">
          <span class="w-14 shrink-0 tabular-nums {b.yours ? 'font-semibold' : 'text-base-content/60'}">{b.start.toFixed(1)}–{(b.start + 0.5).toFixed(1)}</span>
          <div class="flex-1 h-2 rounded-full bg-base-content/10 overflow-hidden" aria-hidden="true">
            <div class="h-full rounded-full {b.yours ? 'bg-primary' : 'bg-base-content/30'}" style="width: {b.win_rate * 100}%"></div>
          </div>
          <span class="w-20 shrink-0 text-right tabular-nums {b.yours ? 'font-semibold' : ''}">{pct1(b.win_rate)}{b.yours ? (them ? " theirs" : " yours") : ""}</span>
        </li>
      {/each}
    </ul>
    <p class="text-sm font-semibold mt-3 {v.costs_wins ? 'text-error' : 'text-success'}">{v.costs_wins ? "Decks at this cost win less often" : "Decks at this cost win as often as any"}</p>
    <p class="text-xs text-base-content/60 mt-0.5">Win rate by deck cost, across all players</p>
  {/if}
</div>
