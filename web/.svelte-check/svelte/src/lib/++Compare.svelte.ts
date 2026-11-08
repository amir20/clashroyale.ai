///<reference types="svelte" />
;;
  type Side = { label: string; value: number | null };;
;type $$ComponentProps =  { title: string; a: Side; b: Side; digits?: number };function $$render() {


  let { title, a, b, digits = 0 }:/*Ωignore_startΩ*/$$ComponentProps/*Ωignore_endΩ*/ = $props();
  const show = (s: Side) => (s.value == null ? "—" : s.value.toFixed(digits));
;
async () => {

 { svelteHTML.createElement("div", { "class":`flex items-center justify-between gap-4 py-3.5`,});
   { svelteHTML.createElement("span", { "class":`text-sm text-base-content/70`,});title; }
   { svelteHTML.createElement("span", { "class":`flex shrink-0 gap-5 text-right tabular-nums`,});
     { svelteHTML.createElement("span", {}); { svelteHTML.createElement("span", { "class":`font-semibold text-primary`,});show(a); }  { svelteHTML.createElement("span", { "class":`text-xs text-base-content/60`,});a.label; } }
     { svelteHTML.createElement("span", {}); { svelteHTML.createElement("span", { "class":`font-semibold`,});show(b); }  { svelteHTML.createElement("span", { "class":`text-xs text-base-content/60`,});b.label; } }
   }
 }
};
return { props: {} as any as $$ComponentProps, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const Compare__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type Compare__SvelteComponent_ = ReturnType<typeof Compare__SvelteComponent_>;
/*Ωignore_endΩ*/export default Compare__SvelteComponent_;