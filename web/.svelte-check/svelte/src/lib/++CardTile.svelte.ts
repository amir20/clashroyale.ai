///<reference types="svelte" />
;
import type { Card } from "./api";

;type $$ComponentProps =  { card: Card; sub?: string; meta?: string; status?: "evolution" | "hero" | "both" | null };function $$render() {

  
  let { card, sub, meta, status }:/*Ωignore_startΩ*/$$ComponentProps/*Ωignore_endΩ*/ =
    $props();
;
async () => {

 { svelteHTML.createElement("figure", {});
  if(card.icon){
     { svelteHTML.createElement("img", {        "src":card.icon,"alt":card.name,"loading":`lazy`,"class":`w-full`,});}
  }else{
     { svelteHTML.createElement("div", { "class":`aspect-[5/6] rounded-lg bg-base-300 grid place-items-center text-xs p-1 text-center`,});card.name; }
  }
  if(sub){ { svelteHTML.createElement("figcaption", { "class":`mt-2 text-center text-xs leading-tight`,});sub; }}
  if(status){
     { svelteHTML.createElement("div", { "class":`mt-0.5 text-center text-[11px] font-semibold leading-tight ${status === 'evolution' ? 'text-accent' : 'text-primary'}`,});status === "hero" ? "Hero" : status === "both" ? "Evolution + Hero" : "Evolution"; }
  }
  if(meta){ { svelteHTML.createElement("div", { "class":`mt-0.5 text-center text-[11px] leading-tight text-base-content/60`,});meta; }}
 }
};
return { props: {} as any as $$ComponentProps, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const CardTile__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type CardTile__SvelteComponent_ = ReturnType<typeof CardTile__SvelteComponent_>;
/*Ωignore_endΩ*/export default CardTile__SvelteComponent_;