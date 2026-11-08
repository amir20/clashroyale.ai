///<reference types="svelte" />
;;
  export type Mood = "idle" | "thinking" | "happy" | "smug" | "oops";;
;type $$ComponentProps =  { mood?: Mood; size?: number };function $$render() {


  let { mood = "idle", size = 160 }:/*Ωignore_startΩ*/$$ComponentProps/*Ωignore_endΩ*/ = $props();
;
async () => {


 { svelteHTML.createElement("svg", {           "class":`rook ${mood}`,"width":size,"height":size,"viewBox":`0 0 200 200`,"role":`img`,"aria-label":`Rook the crow`,});
   { svelteHTML.createElement("ellipse", {          "class":`shadow`,"cx":`100`,"cy":`186`,"rx":`52`,"ry":`8`,});}
   { svelteHTML.createElement("g", { "class":`whole`,});
     { svelteHTML.createElement("path", {    "class":`foot`,"d":`M78 168 l-8 14 M78 168 l2 15 M78 168 l10 13`,});}
     { svelteHTML.createElement("path", {    "class":`foot`,"d":`M122 168 l-10 13 M122 168 l-2 15 M122 168 l8 14`,});}
     { svelteHTML.createElement("ellipse", {          "cx":`100`,"cy":`124`,"rx":`56`,"ry":`54`,"fill":`#4a54a8`,});}
     { svelteHTML.createElement("ellipse", {          "cx":`100`,"cy":`140`,"rx":`33`,"ry":`30`,"fill":`#7f89d6`,});}
     { svelteHTML.createElement("path", {      "class":`wing`,"d":`M50 106 C24 122 26 158 54 164 C62 146 62 124 50 106Z`,"fill":`#333b85`,});}
     { svelteHTML.createElement("g", { "class":`head`,});
       { svelteHTML.createElement("path", {    "d":`M100 18 l-12 22 l14 -6 l10 -20 z`,"fill":`#333b85`,});}
       { svelteHTML.createElement("circle", {        "cx":`100`,"cy":`72`,"r":`46`,"fill":`#4a54a8`,});}
       { svelteHTML.createElement("g", { "class":`eyes`,});
         { svelteHTML.createElement("g", { "class":`eye`,}); { svelteHTML.createElement("circle", {        "cx":`82`,"cy":`66`,"r":`15`,"fill":`#fff`,});} { svelteHTML.createElement("circle", {          "class":`pupil`,"cx":`86`,"cy":`68`,"r":`7`,"fill":`#12142b`,});} }
         { svelteHTML.createElement("g", { "class":`eye`,}); { svelteHTML.createElement("circle", {        "cx":`119`,"cy":`66`,"r":`15`,"fill":`#fff`,});} { svelteHTML.createElement("circle", {          "class":`pupil`,"cx":`123`,"cy":`68`,"r":`7`,"fill":`#12142b`,});} }
       }
       { svelteHTML.createElement("path", {    "class":`brow`,"d":`M66 49 L96 54`,});}
       { svelteHTML.createElement("path", {    "class":`brow right`,"d":`M136 49 L106 54`,});}
       { svelteHTML.createElement("path", {      "class":`beak`,"d":`M86 82 Q101 74 116 82 L101 108 Z`,"fill":`#ffc53d`,});}
       { svelteHTML.createElement("path", {          "d":`M90 90 Q101 94 112 90`,"stroke":`#b87a08`,"stroke-width":`3`,"fill":`none`,"stroke-linecap":`round`,});}
     }
     { svelteHTML.createElement("path", {    "d":`M60 110 Q100 132 140 110 L142 126 Q100 148 58 126 Z`,"fill":`#ff6b4a`,});}
     { svelteHTML.createElement("path", {      "class":`scarf`,"d":`M122 128 l20 40 l-22 -8 l-10 10 z`,"fill":`#e8502f`,});}
   }
  if(mood === "thinking"){
     { svelteHTML.createElement("g", { "class":`thought`,}); { svelteHTML.createElement("circle", {      "cx":`165`,"cy":`44`,"r":`5`,});} { svelteHTML.createElement("circle", {      "cx":`178`,"cy":`30`,"r":`7`,});} { svelteHTML.createElement("circle", {      "cx":`186`,"cy":`12`,"r":`9`,});} }
  }
 }


};
return { props: {} as any as $$ComponentProps, exports: {}, bindings: __sveltets_$$bindings(''), slots: {}, events: {} }}
const Rook__SvelteComponent_ = __sveltets_2_fn_component($$render());
/*Ωignore_startΩ*/type Rook__SvelteComponent_ = ReturnType<typeof Rook__SvelteComponent_>;
/*Ωignore_endΩ*/export default Rook__SvelteComponent_;