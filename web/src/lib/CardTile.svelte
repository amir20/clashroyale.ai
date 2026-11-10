<script lang="ts">
  import type { Card } from "./api";
  // `dim` grays out a card the player already runs, so the different cards in a deck stand out.
  let { card, sub, meta, status, dim = false }: { card: Card; sub?: string; meta?: string; status?: "evolution" | "hero" | "both" | null; dim?: boolean } =
    $props();
  let failedIcon = $state<string | null>(null); // the game's server is missing some card art
</script>

<figure class={dim ? "grayscale opacity-40" : ""} data-dim={dim || undefined}>
  {#if card.icon && failedIcon !== card.icon}
    <img src={card.icon} alt={card.name} loading="lazy" class="w-full" onerror={() => (failedIcon = card.icon)} />
  {:else}
    <div class="aspect-[5/6] rounded-lg bg-base-300 grid place-items-center text-xs p-1 text-center">{card.name}</div>
  {/if}
  {#if sub}<figcaption class="mt-2 text-center text-xs leading-tight">{sub}</figcaption>{/if}
  {#if status}
    <div class="mt-0.5 text-center text-[11px] font-semibold leading-tight {status === 'evolution' ? 'text-accent' : 'text-primary'}">{status === "hero" ? "Hero" : status === "both" ? "Evolution + Hero" : "Evolution"}</div>
  {/if}
  {#if meta}<div class="mt-0.5 text-center text-[11px] leading-tight text-base-content/60">{meta}</div>{/if}
</figure>
