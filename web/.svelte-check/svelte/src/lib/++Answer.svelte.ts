///<reference types="svelte" />
;
import CardTile from "./CardTile.svelte";

;type $$ComponentProps =  { id: string; data: any };function $$render() {

  
  let { id, data }:/*Ωignore_startΩ*/$$ComponentProps/*Ωignore_endΩ*/ = $props();
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const verdict = (c: any) =>
    !c.real
      ? { label: "No difference", tone: "badge-ghost" }
      : c.gain > 0
        ? { label: "Helps", tone: "badge-success" }
        : { label: "Hurts", tone: "badge-error" };
  const versus = (c: any) =>
    !c.real ? "Too few games to tell" : c.diff > 0 ? "Better than most" : "Worse than most";
  const sureness = (games: number) => (games < 30 ? `${games} games, too early to be sure` : `${games} games`);
;
async () => {

if(id === "replace_card" && data.enough){
   { svelteHTML.createElement("p", { "class":`text-sm opacity-80 mb-3`,});
     data.family_decks.toLocaleString();          
    data.family_players.toLocaleString();   
   }
   { svelteHTML.createElement("ul", { "class":`space-y-3`,});
       for(let c of __sveltets_2_ensureArray(data.yours)){c.id;
      const v = verdict(c);
       { svelteHTML.createElement("li", { "class":`flex items-center gap-3`,});
         { svelteHTML.createElement("div", { "class":`w-10 shrink-0`,}); { const $$_eliTdraC3C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC3C({ target: __sveltets_2_any(), props: {  "card":c,}});} }
         { svelteHTML.createElement("div", { "class":`flex-1 min-w-0`,});
           { svelteHTML.createElement("div", { "class":`truncate`,});c.name; }
           { svelteHTML.createElement("div", { "class":`text-xs opacity-70 tabular-nums whitespace-nowrap`,});pct(c.win_rate_with);   pct(c.win_rate_without);  }
         }
         { svelteHTML.createElement("span", { "class":`badge badge-sm badge-soft whitespace-nowrap ${v.tone}`,});v.label; }
       }
    }
   }
  if(data.candidates.length){
     { svelteHTML.createElement("h4", { "class":`text-sm font-semibold mt-5 mb-2`,});   }
     { svelteHTML.createElement("div", { "class":`grid grid-cols-4 gap-2 max-w-md`,});
         for(let c of __sveltets_2_ensureArray(data.candidates)){c.id; { const $$_eliTdraC1C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC1C({ target: __sveltets_2_any(), props: {    "card":c,"sub":`wins ${pct(c.win_rate_with)} · ${c.players} players`,}});}}
     }
  }
} else if (id === "best_cards"){
   { svelteHTML.createElement("ul", { "class":`space-y-3`,});
       for(let c of __sveltets_2_ensureArray(data.cards)){c.id;
       { svelteHTML.createElement("li", { "class":`flex items-center gap-3`,});
         { svelteHTML.createElement("div", { "class":`w-10 shrink-0`,}); { const $$_eliTdraC3C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC3C({ target: __sveltets_2_any(), props: {  "card":c,}});} }
         { svelteHTML.createElement("div", { "class":`flex-1 min-w-0`,});
           { svelteHTML.createElement("div", { "class":`flex justify-between text-sm gap-2`,}); { svelteHTML.createElement("span", { "class":`truncate`,});c.name; } { svelteHTML.createElement("span", { "class":`tabular-nums whitespace-nowrap`,}); pct(c.your_win_rate);  { svelteHTML.createElement("span", { "class":`opacity-60`,});  pct(c.field_win_rate); } } }
           { svelteHTML.createElement("progress", {     "class":`progress progress-primary h-2 w-full`,"value":c.your_win_rate * 100,"max":`100`,}); }
           { svelteHTML.createElement("div", { "class":`text-xs opacity-70`,});c.games;   versus(c); }
         }
       }
    }
   }
} else if (id === "nemesis"){
   { svelteHTML.createElement("h4", { "class":`text-sm font-semibold mb-2 text-error`,});   }
   { svelteHTML.createElement("div", { "class":`grid grid-cols-5 gap-2 max-w-lg`,});
       for(let c of __sveltets_2_ensureArray(data.worst)){c.id; { const $$_eliTdraC1C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC1C({ target: __sveltets_2_any(), props: {    "card":c,"sub":`won ${c.wins} of ${c.games}`,}});}}
   }
  if(data.best.length){
     { svelteHTML.createElement("h4", { "class":`text-sm font-semibold mt-5 mb-2 text-success`,});  }
     { svelteHTML.createElement("div", { "class":`grid grid-cols-5 gap-2 max-w-lg`,});
         for(let c of __sveltets_2_ensureArray(data.best)){c.id; { const $$_eliTdraC1C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC1C({ target: __sveltets_2_any(), props: {    "card":c,"sub":`won ${c.wins} of ${c.games}`,}});}}
     }
  }
} else if (id === "best_deck"){
   { svelteHTML.createElement("ul", { "class":`space-y-4`,});
        for(let d of __sveltets_2_ensureArray(data.decks)){let i = 1;i;
       { svelteHTML.createElement("li", {});
         { svelteHTML.createElement("div", { "class":`flex justify-between text-sm mb-1 gap-2`,});
           { svelteHTML.createElement("span", {});d.wins; d.losses;   pct(d.win_rate); if(d.current){ { svelteHTML.createElement("span", { "class":`badge badge-accent badge-xs ml-1`,});  }} }
           { svelteHTML.createElement("span", { "class":`tabular-nums opacity-70 whitespace-nowrap`,});d.net_trophies > 0 ? "+" : "";d.net_trophies;  }
         }
         { svelteHTML.createElement("div", { "class":`grid grid-cols-8 gap-1 max-w-xl`,});   for(let c of __sveltets_2_ensureArray(d.cards)){c.id; { const $$_eliTdraC3C = __sveltets_2_ensureComponent(CardTile); new $$_eliTdraC3C({ target: __sveltets_2_any(), props: {  "card":c,}});}} }
         { svelteHTML.createElement("div", { "class":`text-xs opacity-70 mt-1`,});sureness(d.games); }
       }
    }
   }
}
};
return { props: {} as any as $$ComponentProps, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const Answer__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type Answer__SvelteComponent_ = ReturnType<typeof Answer__SvelteComponent_>;
/*Ωignore_endΩ*/export default Answer__SvelteComponent_;