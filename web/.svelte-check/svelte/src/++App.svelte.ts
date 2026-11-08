///<reference types="svelte" />
;
import { onMount, tick } from "svelte";
import { cleanTag, loadPlayer, type LoadEvent, type Report } from "./lib/api";
import Ask from "./lib/Ask.svelte";
import CardTile from "./lib/CardTile.svelte";
import Compare from "./lib/Compare.svelte";
import { reveal } from "./lib/reveal";
import Rook, { type Mood } from "./lib/Rook.svelte";
function $$render() {

  
  
  
  
  
  
  

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
  ;
async () => {

 { svelteHTML.createElement("main", { "class":`sky min-h-svh`,});
   { svelteHTML.createElement("section", {  "class":`sticky top-0 h-svh flex flex-col items-center justify-center px-6 text-center`,});phase === "report";
     { const $$_kooR2C = __sveltets_2_ensureComponent(Rook); new $$_kooR2C({ target: __sveltets_2_any(), props: {   mood,"size":phase === "loading" ? 200 : 170,}});}
     { svelteHTML.createElement("h1", { "class":`text-5xl mt-2`,});  }
     { svelteHTML.createElement("p", {    "class":`mt-3 max-w-xs min-h-14 text-lg leading-snug`,"aria-live":`polite`,});!!error;heroLine; }

    if(phase === "loading"){
       { svelteHTML.createElement("div", { "class":`w-full max-w-xs mt-5 pop`,});
         { svelteHTML.createElement("progress", {     "class":`progress progress-primary w-full h-3`,"value":progress,"max":`100`,}); }
         { svelteHTML.createElement("p", { "class":`text-sm opacity-60 mt-2 dots`,}); tag; }
       }
    } else if (phase === "landing"){
       { svelteHTML.createElement("form", {    "class":`w-full max-w-xs mt-5`,"onsubmit":(e) => { e.preventDefault(); start(input); },});shaking;
         { svelteHTML.createElement("label", { "class":`input input-lg w-full`,});
           { svelteHTML.createElement("span", { "class":`opacity-50`,});  }
           { svelteHTML.createElement("input", {              "class":`uppercase`,"placeholder":`YOUR TAG`,"autocomplete":`off`,"autocapitalize":`characters`,"spellcheck":`false`,"aria-label":`Your player tag`,"bind:value":input,});/*Ωignore_startΩ*/() => input = __sveltets_2_any(null);/*Ωignore_endΩ*/}
         }
         { svelteHTML.createElement("button", { "class":`btn btn-primary btn-lg btn-game w-full mt-4 font-semibold`,});   }
         { svelteHTML.createElement("p", { "class":`text-xs opacity-60 mt-3`,});          }
       }
    }
   }

  if(report && o && p){
     { const $$_div1 = svelteHTML.createElement("div", {   "class":`relative z-10 bg-base-100 rounded-t-3xl border-t border-base-content/10 shadow-[0_-20px_50px_#0008]`,});sheet = $$_div1;
       { svelteHTML.createElement("div", { "class":`mx-auto max-w-2xl px-6 pt-3 pb-12 space-y-16`,});
         { svelteHTML.createElement("div", { "class":`flex justify-center`,}); { svelteHTML.createElement("span", { "class":`h-1 w-10 rounded-full bg-base-content/20`,}); } }

         {const $$action_0 = __sveltets_2_ensureAction(reveal(svelteHTML.mapElementTag('section')));{ svelteHTML.createElement("section", __sveltets_2_union($$action_0), { });
           { svelteHTML.createElement("div", { "class":`flex items-start justify-between gap-3`,});
             { svelteHTML.createElement("div", { "class":`min-w-0`,});
               { svelteHTML.createElement("h2", { "class":`text-3xl truncate`,});o.player.name; }
               { svelteHTML.createElement("p", { "class":`text-sm text-base-content/60 truncate`,});o.player.clan ?? "No clan"; }
             }
             { svelteHTML.createElement("button", {   "class":`btn btn-sm btn-ghost shrink-0 font-medium cursor-pointer`,"onclick":reset,});  }
           }

          if(report.similar){
             { svelteHTML.createElement("p", {   "class":`mt-8 text-sm text-base-content/70`,...__sveltets_2_empty({"data-testid":`similar`}),});
              if(report.similar.enough){    { svelteHTML.createElement("span", { "class":`font-semibold text-base-content`,});report.similar.battles.toLocaleString();   }
                 report.similar.players.toLocaleString();     report.similar.days; }else{ report.similar.battles.toLocaleString();         }
             }
          }
           { svelteHTML.createElement("p", { "class":`says mt-4 text-lg`,});report.intro; }

           { svelteHTML.createElement("div", { "class":`mt-8 grid grid-cols-3 divide-x divide-base-content/10 text-center`,});
             { svelteHTML.createElement("div", {}); { svelteHTML.createElement("div", { "class":`stat-num text-2xl text-primary`,});o.player.trophies?.toLocaleString() ?? "—"; } { svelteHTML.createElement("div", { "class":`text-xs text-base-content/60 mt-1`,});  } }
             { svelteHTML.createElement("div", {}); { svelteHTML.createElement("div", { "class":`stat-num text-2xl`,});Math.round(o.record.win_rate * 100);  } { svelteHTML.createElement("div", { "class":`text-xs text-base-content/60 mt-1`,}); o.record.wins;  o.record.games; } }
             { svelteHTML.createElement("div", {}); { svelteHTML.createElement("div", {   "class":`stat-num text-2xl`,});o.net_trophies > 0;o.net_trophies < 0;o.net_trophies > 0 ? "+" : "";o.net_trophies; } { svelteHTML.createElement("div", { "class":`text-xs text-base-content/60 mt-1`,});  } }
           }

           { svelteHTML.createElement("div", { "class":`mt-8`,});
             { svelteHTML.createElement("div", { "class":`flex justify-between text-xs text-base-content/60 mb-2`,}); { svelteHTML.createElement("span", {}); o.recent.length;  } { svelteHTML.createElement("span", {});  } }
             { svelteHTML.createElement("div", {   "class":`flex gap-1`,"aria-label":`Recent results, oldest to latest`,});
                  for(let r of __sveltets_2_ensureArray([...o.recent].reverse())){let i = 1;i;
                 { svelteHTML.createElement("span", {   "class":`flex-1 h-1.5 rounded-full ${r === 'win' ? 'bg-success' : r === 'loss' ? 'bg-error' : 'bg-warning'}`,"title":r,}); }
              }
             }
             { svelteHTML.createElement("div", { "class":`flex gap-4 mt-2 text-xs text-base-content/60`,});
               { svelteHTML.createElement("span", { "class":`flex items-center gap-1.5`,}); { svelteHTML.createElement("span", { "class":`size-2 rounded-full bg-success`,}); }  }
               { svelteHTML.createElement("span", { "class":`flex items-center gap-1.5`,}); { svelteHTML.createElement("span", { "class":`size-2 rounded-full bg-error`,}); }  }
             }
           }
         }}

         {const $$action_0 = __sveltets_2_ensureAction(reveal(svelteHTML.mapElementTag('section')));{ svelteHTML.createElement("section", __sveltets_2_union($$action_0), { });
           { svelteHTML.createElement("div", { "class":`flex items-baseline justify-between`,});
             { svelteHTML.createElement("h2", { "class":`text-xl`,});  }
             { svelteHTML.createElement("p", { "class":`text-sm text-base-content/60`,});o.avg_elixir;   }
           }
           { svelteHTML.createElement("div", { "class":`grid grid-cols-4 gap-x-4 gap-y-6 mt-5`,});
               for(let c of __sveltets_2_ensureArray(o.deck)){c.id;
               { const $$_eliTdraC5C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC5C({ target: __sveltets_2_any(), props: {        "card":c,"sub":c.name,"status":c.status,"meta":c.level && c.level < 16 ? `Level ${c.level}` : undefined,}});}
            }
           }
         }}

         {const $$action_0 = __sveltets_2_ensureAction(reveal(svelteHTML.mapElementTag('section')));{ svelteHTML.createElement("section", __sveltets_2_union($$action_0), { });
           { svelteHTML.createElement("h2", { "class":`text-xl`,});   }
           { svelteHTML.createElement("p", { "class":`says mt-4`,});report.patterns_line; }
           { svelteHTML.createElement("div", { "class":`mt-5 divide-y divide-base-content/10`,});
             { const $$_erapmoC5C = __sveltets_2_ensureComponent(Compare); new $$_erapmoC5C({ target: __sveltets_2_any(), props: {      "title":`Decided by one crown`,"a":{ label: "won", value: p.close_games.wins },"b":{ label: "lost", value: p.close_games.losses },}});}
             { const $$_erapmoC5C = __sveltets_2_ensureComponent(Compare); new $$_erapmoC5C({ target: __sveltets_2_any(), props: {      "title":`Three-crown finishes`,"a":{ label: "you", value: p.three_crown_wins },"b":{ label: "them", value: p.three_crowned_losses },}});}
             { const $$_erapmoC5C = __sveltets_2_ensureComponent(Compare); new $$_erapmoC5C({ target: __sveltets_2_any(), props: {        "title":`Crowns per game`,"digits":2,"a":{ label: "you", value: p.crowns_for },"b":{ label: "them", value: p.crowns_against },}});}
             { const $$_erapmoC5C = __sveltets_2_ensureComponent(Compare); new $$_erapmoC5C({ target: __sveltets_2_any(), props: {        "title":`Elixir wasted per game`,"digits":1,"a":{ label: "in wins", value: p.elixir_leaked_wins },"b":{ label: "in losses", value: p.elixir_leaked_losses },}});}
           }
         }}

         {const $$action_0 = __sveltets_2_ensureAction(reveal(svelteHTML.mapElementTag('section')));{ svelteHTML.createElement("section", __sveltets_2_union($$action_0), { }); { const $$_ksA4C = __sveltets_2_ensureComponent(Ask); new $$_ksA4C({ target: __sveltets_2_any(), props: {  tag,report,}});} }}

         { svelteHTML.createElement("footer", { "class":`text-xs text-base-content/40 text-center pb-[env(safe-area-inset-bottom)]`,});
                       
           { svelteHTML.createElement("a", {       "class":`underline`,"href":`https://www.supercell.com/fan-content-policy`,"target":`_blank`,"rel":`noreferrer`,});    }
         }
       }
     }
  }
 }
};
return { props: {} as Record<string, never>, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const App__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type App__SvelteComponent_ = ReturnType<typeof App__SvelteComponent_>;
/*Ωignore_endΩ*/export default App__SvelteComponent_;