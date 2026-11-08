<script lang="ts">
  import { cubicOut } from "svelte/easing";
  import { fade, fly } from "svelte/transition";
  import type { Card } from "./api";
  import CardTile from "./CardTile.svelte";

  // Everything you can ask about a card, as buttons, so nobody has to know what to type.
  let {
    card,
    inDeck,
    cards,
    onAsk,
    onClose,
  }: { card: Card; inDeck: boolean; cards: Card[]; onAsk: (question: string) => void; onClose: () => void } = $props();

  const wide = matchMedia("(min-width: 1024px)").matches;
  let picking = $state(false);
  let filter = $state("");
  const matches = $derived(cards.filter((c) => c.id !== card.id && c.name.toLowerCase().includes(filter.trim().toLowerCase())));
  const choices = $derived(
    inDeck
      ? [`How am I doing with ${card.name}?`, `What beats ${card.name}?`]
      : [`How do I do against ${card.name}?`, `What pairs best with ${card.name}?`, `What beats ${card.name}?`],
  );
</script>

<svelte:window onkeydown={(e) => e.key === "Escape" && onClose()} />

<div class="fixed inset-0 z-40 flex flex-col justify-end lg:justify-center lg:items-center lg:p-8">
  <button class="absolute inset-0 bg-black/60 cursor-pointer" aria-label="Close" transition:fade={{ duration: 150 }} onclick={onClose}></button>
  <div class="relative mx-auto w-full max-w-md {picking ? 'lg:max-w-3xl' : ''} rounded-t-3xl lg:rounded-3xl bg-base-200 border-t lg:border border-base-content/10 p-5 lg:p-6 pb-[max(1.25rem,env(safe-area-inset-bottom))] transition-[max-width] duration-200" transition:fly={{ y: wide ? 24 : 400, duration: 260, easing: cubicOut }} role="dialog" aria-modal="true" aria-label={card.name} data-testid="card-sheet">
    <div class="flex items-center gap-4">
      <div class="w-14 shrink-0"><CardTile {card} /></div>
      <h3 class="text-xl flex-1 min-w-0">{picking ? `Compare ${card.name} with…` : card.name}</h3>
      <button class="btn btn-ghost btn-sm btn-circle cursor-pointer" aria-label="Close" onclick={onClose}>✕</button>
    </div>

    {#if picking}
      <input class="input w-full mt-4 text-base" placeholder="Find a card" aria-label="Find a card" bind:value={filter} />
      <div class="grid grid-cols-5 lg:grid-cols-8 gap-2 mt-3 max-h-[52svh] overflow-y-auto pr-1" data-testid="card-picker">
        {#each matches as c (c.id)}
          <button class="cursor-pointer text-left" aria-label={c.name} onclick={() => onAsk(`Should I swap ${card.name} for ${c.name}?`)}>
            <CardTile card={c} />
            <span class="block mt-1 text-[10px] leading-tight text-center text-base-content/70">{c.name}</span>
          </button>
        {/each}
      </div>
    {:else}
      <div class="grid gap-2 mt-4">
        {#each choices as q (q)}
          <button class="panel px-4 py-3.5 text-left text-sm font-medium flex justify-between gap-2 cursor-pointer hover:border-primary/60" onclick={() => onAsk(q)}>
            <span>{q}</span><span class="text-primary" aria-hidden="true">→</span>
          </button>
        {/each}
        {#if inDeck}
          <button class="panel px-4 py-3.5 text-left text-sm font-medium flex justify-between gap-2 cursor-pointer hover:border-primary/60" onclick={() => (picking = true)}>
            <span>Compare it with another card</span><span class="text-primary" aria-hidden="true">→</span>
          </button>
        {/if}
      </div>
    {/if}
  </div>
</div>
