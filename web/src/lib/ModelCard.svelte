<script lang="ts">
  import type { Card, ModelView } from "./api";
  import CardTile from "./CardTile.svelte";

  // The deck model's read on a deck: how it does against the decks at these trophies, and which
  // cards, pairs and matchups make up that number.
  let { model, them = false, onHow, onCard }: { model: ModelView; them?: boolean; onHow: () => void; onCard?: (c: Card) => void } = $props();

  const per100 = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(1)}`;
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const scale = $derived(Math.max(1, ...model.cards.map((c) => Math.abs(c.total))));
  const your = $derived(them ? "their" : "your");
  const label = (c: { name: string; status?: string | null }) =>
    c.name + (c.status === "evolution" ? " (Evo)" : c.status === "hero" ? " (Hero)" : c.status === "both" ? " (Evo + Hero)" : "");
</script>

{#snippet tile(c: Card)}
  {#if onCard}
    <button class="block w-full cursor-pointer transition active:scale-95" aria-label={`More about ${c.name}`} onclick={() => onCard(c)}><CardTile card={c} /></button>
  {:else}
    <CardTile card={c} />
  {/if}
{/snippet}

{#snippet chips(rows: ModelView["strong"], tone: string)}
  <div class="grid grid-cols-3 gap-2">
    {#each rows as c (c.id * 10 + (c.status ? 1 : 0))}
      <div class="text-center">
        <div class="w-12 mx-auto">{@render tile(c)}</div>
        <div class="stat-num text-sm mt-1 {tone}">{per100(c.effect)}</div>
        <div class="text-[11px] text-base-content/60 leading-tight">in {pct(c.share)} of decks</div>
      </div>
    {/each}
  </div>
{/snippet}

<div class="flex flex-col gap-4" data-testid="model">
  <div class="flex items-end gap-3">
    <span class="stat-num text-5xl text-primary" data-testid="model-win">{pct(model.win)}</span>
    <span class="text-sm text-base-content/70 leading-snug pb-1">predicted win chance against decks {them ? model.field.where.replace("your", "their") : model.field.where}, if card levels were equal</span>
  </div>

  <div>
    <h3 class="text-sm font-semibold mb-2">What each card adds <span class="font-normal text-base-content/60">(wins per 100 games vs an average card)</span></h3>
    <ul class="grid gap-1.5" data-testid="model-cards">
      {#each model.cards as c (c.id)}
        <li class="grid grid-cols-[2rem_1fr_auto] items-center gap-2.5">
          <div class="w-8">{@render tile(c)}</div>
          <div class="min-w-0">
            <div class="text-xs truncate">{label(c)}</div>
            <div class="relative h-2 mt-1 rounded-full bg-base-content/10">
              <span class="absolute inset-y-0 left-1/2 w-px bg-base-content/30"></span>
              <span
                class="absolute inset-y-0 rounded-full {c.total >= 0 ? 'bg-success left-1/2' : 'bg-error right-1/2'}"
                style="width: {(Math.abs(c.total) / scale) * 50}%"
              ></span>
            </div>
            <div class="text-[10px] text-base-content/50 mt-0.5 tabular-nums">alone {per100(c.alone)} · with the rest {per100(c.synergy)} · matchups {per100(c.matchups)}</div>
          </div>
          <span class="stat-num text-sm w-10 text-right {c.total >= 0 ? 'text-success' : 'text-error'}">{per100(c.total)}</span>
        </li>
      {/each}
    </ul>
  </div>

  {#if model.strong.length || model.weak.length}
    <div class="grid grid-cols-2 gap-4">
      {#if model.strong.length}<div><h3 class="text-sm font-semibold mb-2">Best against</h3>{@render chips(model.strong, "text-success")}</div>{/if}
      {#if model.weak.length}<div><h3 class="text-sm font-semibold mb-2">Worst against</h3>{@render chips(model.weak, "text-error")}</div>{/if}
    </div>
  {/if}

  {#if model.synergy.length}
    <div class="text-xs text-base-content/70" data-testid="model-synergy">
      <span class="font-semibold text-base-content/80">Works together:</span>
      {model.synergy.map((p) => `${p.cards.map(label).join(" + ")} (${per100(p.effect)})`).join(", ")}
      {#if model.clash.length}<br /><span class="font-semibold text-base-content/80">Pulls against each other:</span> {model.clash.map((p) => `${p.cards.map(label).join(" + ")} (${per100(p.effect)})`).join(", ")}{/if}
    </div>
  {/if}

  {#if model.replay}
    {@const r = model.replay}
    {@const edge = r.wins - r.expected}
    <p class="text-sm text-base-content/75" data-testid="beat-odds">
      <span class="font-semibold text-base-content/90">Against {your} last {r.games} opponents</span> with this deck, the model expected {r.expected.toFixed(1)} wins, counting card levels. {them ? "They" : "You"} won {r.wins}{#if Math.abs(edge) >= 1.5}: <b class={edge > 0 ? "text-success" : "text-error"}>{edge > 0 ? `${edge.toFixed(1)} more than expected` : `${(-edge).toFixed(1)} fewer than expected`}</b>{edge > 0 ? (them ? ", so they play it better than most." : ", so you play it better than most.") : (them ? ", so the deck isn't the whole story." : ", so the deck isn't the whole story: play matters too.")}{:else}, about what the model expected.{/if}
    </p>
  {/if}

  <button class="text-sm link link-primary text-left cursor-pointer w-fit" onclick={onHow} data-testid="model-how">How does Rook predict this?</button>
</div>
