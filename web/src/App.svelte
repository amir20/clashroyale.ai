<script lang="ts">
  import { onMount, tick } from "svelte";
  import { cleanTag, loadPlayer, type LoadEvent, type Report } from "./lib/api";
  import Ask from "./lib/Ask.svelte";
  import CardTile from "./lib/CardTile.svelte";
  import Compare from "./lib/Compare.svelte";
  import { reveal } from "./lib/reveal";
  import Rook, { type Mood } from "./lib/Rook.svelte";

  type Phase = "landing" | "loading" | "report";
  let phase = $state<Phase>("landing");
  let input = $state("");
  let status = $state("");
  let progress = $state(0);
  let error = $state("");
  let shaking = $state(false);
  let tag = $state("");
  let report = $state<Report | null>(null);
  let sheet = $state<HTMLElement>();
  let stop: (() => void) | undefined;

  const mood = $derived<Mood>(
    error ? "oops" : phase === "loading" ? "thinking" : phase === "report" ? "happy" : "idle",
  );
  const heroLine = $derived(
    error ||
      (phase === "loading"
        ? status
        : phase === "report"
          ? "There you are. Let's see the damage."
          : "Give me your tag and I'll tell you the truth about your deck."),
  );

  function fail(message: string) {
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
      phase = "report";
      localStorage.setItem("rook-tag", tag);
      history.replaceState(null, "", `?tag=${tag}`);
      await tick();
      // Slide the report up over the hero, like an Apple product page.
      requestAnimationFrame(() => sheet?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }

  function start(raw: string) {
    const cleaned = cleanTag(raw);
    if (cleaned.length < 3) return fail("That's too short to be a tag. Nice try.");
    stop?.();
    tag = cleaned;
    input = cleaned;
    error = "";
    report = null;
    progress = 3;
    status = "Waking up…";
    phase = "loading";
    stop = loadPlayer(cleaned, onEvent);
  }

  function reset() {
    stop?.();
    phase = "landing";
    report = null;
    history.replaceState(null, "", location.pathname);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  onMount(() => {
    const fromUrl = new URLSearchParams(location.search).get("tag");
    if (fromUrl) start(fromUrl);
    else input = localStorage.getItem("rook-tag") ?? "";
    return () => stop?.();
  });

  const o = $derived(report?.overview);
  const p = $derived(report?.patterns);
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

  {#if report && o && p}
    <div bind:this={sheet} class="relative z-10 bg-base-100 rounded-t-3xl border-t border-base-content/10 shadow-[0_-20px_50px_#0008]">
      <div class="mx-auto max-w-2xl px-6 pt-3 pb-12 space-y-16">
        <div class="flex justify-center"><span class="h-1 w-10 rounded-full bg-base-content/20"></span></div>

        <section use:reveal>
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <h2 class="text-3xl truncate">{o.player.name}</h2>
              <p class="text-sm text-base-content/60 truncate">{o.player.clan ?? "No clan"}</p>
            </div>
            <button class="btn btn-sm btn-ghost shrink-0 font-medium cursor-pointer" onclick={reset}>Not you?</button>
          </div>

          {#if report.similar}
            <p class="mt-8 text-sm text-base-content/70" data-testid="similar">
              {#if report.similar.enough}
                Comparing you with <span class="font-semibold text-base-content">{report.similar.battles.toLocaleString()} similar battles</span>
                from {report.similar.players.toLocaleString()} players in the last {report.similar.days} days.
              {:else}
                Only {report.similar.battles.toLocaleString()} similar battles so far, so deck comparisons are limited.
              {/if}
            </p>
          {/if}
          <p class="says mt-4 text-lg">{report.intro}</p>

          <div class="mt-8 grid grid-cols-3 divide-x divide-base-content/10 text-center">
            <div><div class="stat-num text-2xl text-primary">{o.player.trophies?.toLocaleString() ?? "—"}</div><div class="text-xs text-base-content/60 mt-1">Trophies</div></div>
            <div><div class="stat-num text-2xl">{Math.round(o.record.win_rate * 100)}%</div><div class="text-xs text-base-content/60 mt-1">Won {o.record.wins} of {o.record.games}</div></div>
            <div><div class="stat-num text-2xl" class:text-success={o.net_trophies > 0} class:text-error={o.net_trophies < 0}>{o.net_trophies > 0 ? "+" : ""}{o.net_trophies}</div><div class="text-xs text-base-content/60 mt-1">Trophies gained</div></div>
          </div>

          <div class="mt-8">
            <div class="flex justify-between text-xs text-base-content/60 mb-2"><span>Last {o.recent.length} games</span><span>Latest →</span></div>
            <div class="flex gap-1" aria-label="Recent results, oldest to latest">
              {#each [...o.recent].reverse() as r, i (i)}
                <span class="flex-1 h-1.5 rounded-full {r === 'win' ? 'bg-success' : r === 'loss' ? 'bg-error' : 'bg-warning'}" title={r}></span>
              {/each}
            </div>
            <div class="flex gap-4 mt-2 text-xs text-base-content/60">
              <span class="flex items-center gap-1.5"><span class="size-2 rounded-full bg-success"></span>Win</span>
              <span class="flex items-center gap-1.5"><span class="size-2 rounded-full bg-error"></span>Loss</span>
            </div>
          </div>
        </section>

        <section use:reveal>
          <div class="flex items-baseline justify-between">
            <h2 class="text-xl">Your deck</h2>
            <p class="text-sm text-base-content/60">{o.avg_elixir} average elixir</p>
          </div>
          <div class="grid grid-cols-4 gap-x-4 gap-y-6 mt-5">
            {#each o.deck as c (c.id)}
              <CardTile card={c} sub={c.name} status={c.status} meta={c.level && c.level < 16 ? `Level ${c.level}` : undefined} />
            {/each}
          </div>
        </section>

        <section use:reveal>
          <h2 class="text-xl">Wins and losses</h2>
          <p class="says mt-4">{report.patterns_line}</p>
          <div class="mt-5 divide-y divide-base-content/10">
            <Compare title="Decided by one crown" a={{ label: "won", value: p.close_games.wins }} b={{ label: "lost", value: p.close_games.losses }} />
            <Compare title="Three-crown finishes" a={{ label: "you", value: p.three_crown_wins }} b={{ label: "them", value: p.three_crowned_losses }} />
            <Compare title="Crowns per game" digits={2} a={{ label: "you", value: p.crowns_for }} b={{ label: "them", value: p.crowns_against }} />
            <Compare title="Elixir wasted per game" digits={1} a={{ label: "in wins", value: p.elixir_leaked_wins }} b={{ label: "in losses", value: p.elixir_leaked_losses }} />
          </div>
        </section>

        <section use:reveal><Ask {tag} {report} /></section>

        <footer class="text-xs text-base-content/40 text-center pb-[env(safe-area-inset-bottom)]">
          This material is unofficial and is not endorsed by Supercell. For more information see
          <a class="underline" href="https://www.supercell.com/fan-content-policy" target="_blank" rel="noreferrer">Supercell's Fan Content Policy</a>.
        </footer>
      </div>
    </div>
  {/if}
</main>
