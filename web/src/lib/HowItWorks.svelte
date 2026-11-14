<script lang="ts">
  import { cubicOut } from "svelte/easing";
  import { fade, fly } from "svelte/transition";
  import type { ModelView } from "./api";

  // What the deck model is, what it learned from, how often it's right, and how to read it.
  let { about, onClose }: { about: ModelView["about"]; onClose: () => void } = $props();

  const wide = matchMedia("(min-width: 1024px)").matches;
  const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
  const millions = (n: number) => `${(n / 1e6).toFixed(1)} million`;
  const bars = $derived([
    { label: "Coin flip", v: 0.5, tone: "bg-base-content/25" },
    { label: "Card levels and trophies only", v: about.strength_accuracy, tone: "bg-base-content/40" },
    { label: "Rook's model, with both decks", v: about.accuracy, tone: "bg-primary" },
  ]);
</script>

<svelte:window onkeydown={(e) => e.key === "Escape" && onClose()} />

<div class="fixed inset-0 z-40 flex flex-col justify-end lg:justify-center lg:items-center lg:p-8">
  <button class="absolute inset-0 bg-black/60 cursor-pointer" aria-label="Close" transition:fade={{ duration: 150 }} onclick={onClose}></button>
  <div class="relative mx-auto w-full max-w-xl max-h-[88svh] overflow-y-auto rounded-t-3xl lg:rounded-3xl bg-base-200 border-t lg:border border-base-content/10 p-5 lg:p-7 pb-[max(1.25rem,env(safe-area-inset-bottom))]" transition:fly={{ y: wide ? 24 : 400, duration: 260, easing: cubicOut }} role="dialog" aria-modal="true" aria-label="How Rook predicts" data-testid="how-it-works">
    <div class="flex items-center gap-4">
      <h3 class="text-2xl flex-1">How Rook predicts</h3>
      <button class="btn btn-ghost btn-sm btn-circle cursor-pointer" aria-label="Close" onclick={onClose}>✕</button>
    </div>

    <p class="mt-3 text-sm leading-relaxed text-base-content/80">
      Rook learned from <b>{millions(about.battles)} real battles</b> (since {about.first_day}) which decks beat which. Given two decks, it predicts who wins. It adds up three kinds of things it learned:
    </p>

    <!-- The model, as a picture: two decks in, three kinds of weights summed, a win chance out. -->
    <div class="mt-4 grid gap-2 text-sm" aria-label="Diagram: how a prediction is made">
      <div class="grid grid-cols-2 gap-2">
        <div class="rounded-xl border border-primary/40 bg-primary/10 p-2.5 text-center"><b>Your 8 cards</b><div class="text-xs text-base-content/60">as played: Evo and Hero count as their own cards</div></div>
        <div class="rounded-xl border border-base-content/20 bg-base-100 p-2.5 text-center"><b>Their 8 cards</b><div class="text-xs text-base-content/60">from the decks at your trophies</div></div>
      </div>
      <div class="text-center text-base-content/40" aria-hidden="true">↓ ↓</div>
      <div class="rounded-xl border border-base-content/15 bg-base-100 p-3 grid gap-2">
        <div class="flex gap-3 items-baseline"><span class="stat-num text-primary w-5">1</span><span><b>Each card</b>: how much it wins on its own. <span class="text-base-content/60">“Alone”</span></span></div>
        <div class="flex gap-3 items-baseline"><span class="stat-num text-primary w-5">2</span><span><b>Each pair in a deck</b> ({about.pairs.toLocaleString()} learned): cards that work together, or don't. <span class="text-base-content/60">“With the rest”</span></span></div>
        <div class="flex gap-3 items-baseline"><span class="stat-num text-primary w-5">3</span><span><b>Each of your cards against each of theirs</b> ({about.matchups.toLocaleString()} learned): counters. <span class="text-base-content/60">“Matchups”</span></span></div>
        <div class="flex gap-3 items-baseline text-base-content/60"><span class="w-5 text-center">+</span><span>card level and trophy gap, so strong accounts don't make their decks look better than they are</span></div>
      </div>
      <div class="text-center text-base-content/40" aria-hidden="true">↓</div>
      <div class="rounded-xl bg-primary text-primary-content p-2.5 text-center font-semibold">Win chance</div>
    </div>

    <h4 class="mt-6 font-semibold">How often it's right</h4>
    <p class="text-sm text-base-content/70 mt-1">Picking the winner of {about.test_battles.toLocaleString()} battles from {about.test_day}, a day it never trained on:</p>
    <div class="mt-3 grid gap-2" data-testid="accuracy">
      {#each bars as b (b.label)}
        <div class="grid grid-cols-[1fr_auto] gap-x-3 text-xs">
          <span>{b.label}</span><span class="stat-num">{pct(b.v)}</span>
          <div class="col-span-2 h-2.5 mt-1 rounded-full bg-base-content/10 overflow-hidden"><div class="h-full rounded-full {b.tone}" style="width: {b.v * 100}%"></div></div>
        </div>
      {/each}
    </div>
    <p class="text-sm text-base-content/70 mt-3">
      The decks add about {Math.round((about.accuracy - about.strength_accuracy) * 100)} points over levels and trophies. We also tested it on players it had never seen, and it did just as well, so it learned how decks match up, not who the strong players are.
      When it says 60%, about 60% of those battles are won.
    </p>

    <h4 class="mt-6 font-semibold">Reading the numbers</h4>
    <ul class="text-sm text-base-content/70 mt-1 grid gap-1.5 list-disc pl-5">
      <li><b>Wins per 100 games</b>: how many more (or fewer) of 100 games the model expects you to win.</li>
      <li><b>What each card adds</b> is compared with an average card in the decks you face. The bars add up to your deck's edge.</li>
      <li>Predictions assume <b>equal card levels</b>, so they're about the deck, not your account.</li>
      <li>Swap ideas come from battle data (players with your core who swapped). The model's check sits beside each one: when they disagree, be skeptical of both.</li>
    </ul>

    <h4 class="mt-6 font-semibold">What it doesn't know</h4>
    <ul class="text-sm text-base-content/70 mt-1 grid gap-1.5 list-disc pl-5">
      <li>How well you play. Even at {pct(about.accuracy)}, about one battle in three goes the other way.</li>
      <li>Card combos beyond pairs, like a three-card cycle.</li>
      <li>Cards released after {about.published}, until it's retrained.</li>
    </ul>
  </div>
</div>
