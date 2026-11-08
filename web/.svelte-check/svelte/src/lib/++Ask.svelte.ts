///<reference types="svelte" />
;
import { tick } from "svelte";
import { askRook, getAnswer, getSuggestions, type ChatTurn, type Report, type Usage } from "./api";
import Answer from "./Answer.svelte";
import Rook, { type Mood } from "./Rook.svelte";
;

  type Turn = { key: number; q: string; id?: string; status?: string; line?: string; data?: any; failed?: boolean; usage?: Usage; evidence?: string[] };;;
  type Option = { label: string; id?: string };;
;type $$ComponentProps =  { tag: string; report: Report };function $$render() {

  
  
  
  

  let { tag, report }:/*Ωignore_startΩ*/$$ComponentProps/*Ωignore_endΩ*/ = $props();



  let turns = $state<Turn[]>([]);
  // svelte-ignore state_referenced_locally (a new report remounts this component)
  let builtIn = $state<Option[]>(report.questions.map((q) => ({ id: q.id, label: q.label })));
  let suggested = $state<Option[]>([]);
  let thinkingOfMore = $state(false);
  let suggestCost = $state(0);
  let suggestUsage = $state<Usage>();
  let draft = $state("");
  let busy = $state(false);
  let left = $state<number | null>(null);
  let key = 0;
  let suggestRun = 0;

  const digging = ["Digging through your battles…", "Doing the math so you don't have to…", "Checking the receipts…"];
  const mood = $derived<Mood>(busy ? "thinking" : turns.at(-1)?.failed ? "oops" : turns.length ? "smug" : "idle");
  const outOfQuestions = $derived(!report.dev && left === 0);
  const options = $derived([...(outOfQuestions ? [] : suggested), ...builtIn].slice(0, 4));
  const totalCost = $derived(turns.reduce((sum, t) => sum + (t.usage?.cost_usd ?? 0), 0) + suggestCost);
  const usd = (x: number) => `$${x.toFixed(5)}`;
  const usageLine = (u: Usage) =>
    u.model
      ? `${u.model} · ${u.rounds} ${u.rounds === 1 ? "call" : "calls"} · ${u.input_tokens?.toLocaleString()} in / ${u.output_tokens?.toLocaleString()} out · ${usd(u.cost_usd)} · ${u.seconds}s`
      : `no AI used · ${usd(0)} · ${u.seconds}s`;

  function update(k: number, patch: Partial<Turn>) {
    turns = turns.map((t) => (t.key === k ? { ...t, ...patch } : t));
  }

  /** The conversation so far, for the model's memory. */
  function history(): ChatTurn[] {
    const pairs = turns.filter((t) => t.line && !t.failed && !t.status);
    return pairs
      .flatMap((t): ChatTurn[] => [
        { role: "user", content: t.q.slice(0, 1900) },
        { role: "assistant", content: t.line!.slice(0, 1900) },
      ])
      .slice(-12);
  }

  /** Start a new turn: use up the option, show the question, and bring it into view. */
  async function begin(q: string, id?: string, status = "Rook is thinking…") {
    const same = (o: Option) => o.label.trim().toLowerCase() === q.trim().toLowerCase();
    builtIn = builtIn.filter((o) => !same(o));
    suggested = suggested.filter((o) => !same(o));
    suggestRun++; // any suggestions still on their way are now out of date
    thinkingOfMore = false;
    busy = true;
    const k = ++key;
    turns = [...turns, { key: k, q, id, status }];
    await tick();
    document.getElementById(`turn-${k}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return k;
  }

  async function suggestNext() {
    if (!report.ai_enabled || outOfQuestions) return;
    const run = ++suggestRun;
    thinkingOfMore = true;
    try {
      const taken = [...turns.map((t) => t.q), ...builtIn.map((o) => o.label)];
      const res = await getSuggestions(tag, history(), taken);
      if (run !== suggestRun) return;
      const free = new Set(builtIn.map((o) => o.label.toLowerCase()));
      suggested = res.suggestions.filter((q) => !free.has(q.toLowerCase())).map((label) => ({ label }));
      if (res.usage) {
        suggestUsage = res.usage;
        suggestCost += res.usage.cost_usd;
      }
    } catch {
      // The built-in questions are still there.
    } finally {
      if (run === suggestRun) thinkingOfMore = false;
    }
  }

  async function preset(id: string, label: string) {
    if (busy) return;
    const k = await begin(label, id, digging[key % digging.length]);
    try {
      const a = await getAnswer(tag, id);
      update(k, { status: undefined, line: a.line, data: a.data, usage: a.usage, evidence: a.evidence });
    } catch {
      update(k, { status: undefined, line: "Something broke, and for once it wasn't your deck. Try again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  async function custom(q: string) {
    if (q.length < 3 || busy || outOfQuestions) return;
    const earlier = history();
    const k = await begin(q);
    try {
      await askRook(tag, q, earlier, (ev) => {
        if (ev.type === "status") update(k, { status: ev.message });
        else if (ev.type === "answer") {
          update(k, { status: undefined, line: ev.text, usage: ev.usage, evidence: ev.evidence });
          left = ev.left;
        } else update(k, { status: undefined, line: ev.message, failed: true });
      });
    } catch {
      update(k, { status: undefined, line: "I lost the connection. Ask me again.", failed: true });
    }
    // Never leave a question spinning: if the stream ended without an answer, say so.
    if (turns.find((t) => t.key === k)?.status) {
      update(k, { status: undefined, line: "I lost my train of thought. Ask me again.", failed: true });
    }
    busy = false;
    suggestNext();
  }

  function pick(o: Option) {
    if (o.id) preset(o.id, o.label);
    else custom(o.label);
  }

  function submit(e: SubmitEvent) {
    e.preventDefault();
    const q = draft.trim();
    draft = "";
    custom(q);
  }
;
async () => {

 { svelteHTML.createElement("div", { "class":`flex items-center gap-3 mb-4`,});
   { const $$_kooR1C = __sveltets_2_ensureComponent(Rook); new $$_kooR1C({ target: __sveltets_2_any(), props: {   mood,"size":56,}});}
   { svelteHTML.createElement("div", {});
     { svelteHTML.createElement("h2", { "class":`text-xl`,});  }
     { svelteHTML.createElement("p", { "class":`text-sm text-base-content/70`,});      }
   }
 }

 { svelteHTML.createElement("div", {   "class":`space-y-6`,"aria-live":`polite`,});
     for(let t of __sveltets_2_ensureArray(turns)){t.key;
     { svelteHTML.createElement("div", {   "id":`turn-${t.key}`,"class":`pop scroll-mt-4`,});
       { svelteHTML.createElement("p", { "class":`text-sm font-semibold`,});t.q; }
       { svelteHTML.createElement("div", { "class":`panel mt-2 p-4`,});
        if(t.status){
           { svelteHTML.createElement("span", { "class":`flex items-center gap-2 text-sm`,}); { svelteHTML.createElement("span", { "class":`loading loading-dots loading-sm text-primary`,}); }t.status; }
        }else{
           { svelteHTML.createElement("p", { "class":`says`,});t.line; }
          if(t.id && t.data){ { svelteHTML.createElement("div", { "class":`mt-4`,}); { const $$_rewsnA4C = __sveltets_2_ensureComponent(Answer); new $$_rewsnA4C({ target: __sveltets_2_any(), props: {    "id":t.id,"data":t.data,}});} }}
          if(t.evidence?.length){
             { svelteHTML.createElement("p", {   "class":`mt-4 pt-3 border-t border-base-content/10 text-xs text-base-content/70`,...__sveltets_2_empty({"data-testid":`evidence`}),}); { svelteHTML.createElement("span", { "class":`font-semibold text-base-content`,});  } t.evidence.join(" · "); }
          }
        }
       }
      if(t.usage){ { svelteHTML.createElement("p", {   "class":`font-mono text-[11px] text-accent mt-1 px-1 break-words`,...__sveltets_2_empty({"data-testid":`dev-usage`}),});usageLine(t.usage); }}
     }
  }
 }

 { svelteHTML.createElement("div", { "class":turns.length ? "mt-6" : "",});
  if(turns.length && (options.length || thinkingOfMore)){
     { svelteHTML.createElement("p", { "class":`text-sm font-medium mb-2`,});  }
  }
   { svelteHTML.createElement("div", {   "class":`grid gap-2`,...__sveltets_2_empty({"data-testid":`options`}),});
       for(let o of __sveltets_2_ensureArray(options)){o.label;
       { svelteHTML.createElement("button", {     "class":`panel px-4 py-3 text-left text-sm font-medium flex items-center justify-between gap-2 transition cursor-pointer hover:border-primary/60 disabled:opacity-50 disabled:cursor-not-allowed`,"disabled":busy,"onclick":() => pick(o),});
         { svelteHTML.createElement("span", {});o.label; } { svelteHTML.createElement("span", {   "class":`text-primary`,"aria-hidden":`true`,});  }
       }
    }
   }
  if(thinkingOfMore){
     { svelteHTML.createElement("p", {   "class":`flex items-center gap-2 text-sm text-base-content/70 mt-3`,...__sveltets_2_empty({"data-testid":`thinking-more`}),}); { svelteHTML.createElement("span", { "class":`loading loading-dots loading-xs text-primary`,}); }         }
  }
  if(suggestUsage){ { svelteHTML.createElement("p", {   "class":`font-mono text-[11px] text-accent mt-1 px-1 break-words`,...__sveltets_2_empty({"data-testid":`dev-suggest-usage`}),}); usageLine(suggestUsage); }}
 }

 { svelteHTML.createElement("form", {   "class":`mt-6`,"onsubmit":submit,});
   { svelteHTML.createElement("label", {   "class":`text-sm font-medium`,"for":`ask`,});    }
  if(report.ai_enabled){
     { svelteHTML.createElement("div", { "class":`join w-full mt-1`,});
       { svelteHTML.createElement("input", {            "id":`ask`,"class":`input join-item w-full text-base`,"maxlength":300,"placeholder":outOfQuestions ? "That's all for today" : "Should I use Log or Zap?","bind:value":draft,"disabled":busy || outOfQuestions,});/*Ωignore_startΩ*/() => draft = __sveltets_2_any(null);/*Ωignore_endΩ*/}
       { svelteHTML.createElement("button", {   "class":`btn btn-primary join-item cursor-pointer disabled:cursor-not-allowed`,"disabled":busy || outOfQuestions || draft.trim().length < 3,});
        if(busy){ { svelteHTML.createElement("span", { "class":`loading loading-spinner loading-sm`,}); }}else{ }
       }
     }
    if(report.dev){
       { svelteHTML.createElement("p", {   "class":`font-mono text-[11px] text-accent mt-1`,...__sveltets_2_empty({"data-testid":`dev-total`}),});         usd(totalCost); }
    }else{
       { svelteHTML.createElement("p", { "class":`text-xs text-base-content/60 mt-1`,});left ?? report.custom_questions_per_day;  left === 1 ? "question" : "questions";           }
    }
  }else{
     { svelteHTML.createElement("p", { "class":`text-sm text-base-content/60 mt-1`,});           }
  }
 }
};
return { props: {} as any as $$ComponentProps, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const Ask__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type Ask__SvelteComponent_ = ReturnType<typeof Ask__SvelteComponent_>;
/*Ωignore_endΩ*/export default Ask__SvelteComponent_;