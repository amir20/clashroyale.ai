<script lang="ts">
  export type Mood = "idle" | "thinking" | "happy" | "smug" | "oops";
  let { mood = "idle", size = 160 }: { mood?: Mood; size?: number } = $props();
</script>

<!-- Rook is an original character: a small crow with a scarf and opinions. -->
<svg class="rook {mood}" width={size} height={size} viewBox="0 0 200 200" role="img" aria-label="Rook the crow">
  <ellipse class="shadow" cx="100" cy="186" rx="52" ry="8" />
  <g class="whole">
    <path class="foot" d="M78 168 l-8 14 M78 168 l2 15 M78 168 l10 13" />
    <path class="foot" d="M122 168 l-10 13 M122 168 l-2 15 M122 168 l8 14" />
    <ellipse cx="100" cy="124" rx="56" ry="54" fill="#4a54a8" />
    <ellipse cx="100" cy="140" rx="33" ry="30" fill="#7f89d6" />
    <path class="wing" d="M50 106 C24 122 26 158 54 164 C62 146 62 124 50 106Z" fill="#333b85" />
    <g class="head">
      <path d="M100 18 l-12 22 l14 -6 l10 -20 z" fill="#333b85" />
      <circle cx="100" cy="72" r="46" fill="#4a54a8" />
      <g class="eyes">
        <g class="eye"><circle cx="82" cy="66" r="15" fill="#fff" /><circle class="pupil" cx="86" cy="68" r="7" fill="#12142b" /></g>
        <g class="eye"><circle cx="119" cy="66" r="15" fill="#fff" /><circle class="pupil" cx="123" cy="68" r="7" fill="#12142b" /></g>
      </g>
      <path class="brow" d="M66 49 L96 54" />
      <path class="brow right" d="M136 49 L106 54" />
      <path class="beak" d="M86 82 Q101 74 116 82 L101 108 Z" fill="#ffc53d" />
      <path d="M90 90 Q101 94 112 90" stroke="#b87a08" stroke-width="3" fill="none" stroke-linecap="round" />
    </g>
    <path d="M60 110 Q100 132 140 110 L142 126 Q100 148 58 126 Z" fill="#ff6b4a" />
    <path class="scarf" d="M122 128 l20 40 l-22 -8 l-10 10 z" fill="#e8502f" />
  </g>
  {#if mood === "thinking"}
    <g class="thought"><circle cx="165" cy="44" r="5" /><circle cx="178" cy="30" r="7" /><circle cx="186" cy="12" r="9" /></g>
  {/if}
</svg>

<style>
  .rook { overflow: visible; filter: drop-shadow(0 0 22px #7f89d655); }
  .shadow { fill: #0007; }
  .foot { stroke: #ffc53d; stroke-width: 5; stroke-linecap: round; fill: none; }
  .brow { stroke: #12142b; stroke-width: 6; stroke-linecap: round; transform-box: fill-box; transform-origin: center; }
  .whole { transform-origin: 100px 180px; animation: bob 2.4s ease-in-out infinite; }
  .head { transform-origin: 100px 110px; animation: tilt 5s ease-in-out infinite; }
  .eye { transform-box: fill-box; transform-origin: center; animation: blink 4.2s infinite; }
  .wing { transform-origin: 52px 108px; animation: flap 3.5s ease-in-out infinite; }
  .scarf { transform-origin: 122px 128px; animation: flutter 1.8s ease-in-out infinite; }
  .thought circle { fill: #fff; animation: rise 1.4s ease-in-out infinite; }
  .thought circle:nth-child(2) { animation-delay: 0.2s; }
  .thought circle:nth-child(3) { animation-delay: 0.4s; }
  .thinking .head { animation: ponder 1.6s ease-in-out infinite; }
  .thinking .pupil { animation: look 1.6s ease-in-out infinite; }
  .thinking .whole { animation-duration: 1.2s; }
  .happy .whole { animation: hop 0.6s ease-in-out 3; }
  .happy .wing { animation: flap 0.25s ease-in-out infinite; }
  .smug .brow { transform: rotate(-8deg) translateY(-3px); }
  .smug .brow.right { transform: rotate(14deg) translateY(4px); }
  .oops .head { animation: nope 0.35s ease-in-out 2; }
  .oops .brow { transform: rotate(-16deg); }
  .oops .brow.right { transform: rotate(16deg); }
  @keyframes bob { 50% { transform: translateY(-5px) scaleY(1.02); } }
  @keyframes tilt { 0%, 70%, 100% { transform: rotate(0); } 80%, 92% { transform: rotate(-7deg); } }
  @keyframes blink { 0%, 94%, 100% { transform: scaleY(1); } 97% { transform: scaleY(0.08); } }
  @keyframes flap { 50% { transform: rotate(9deg); } }
  @keyframes flutter { 50% { transform: rotate(-9deg) skewX(4deg); } }
  @keyframes ponder { 50% { transform: rotate(9deg) translateY(2px); } }
  @keyframes look { 50% { transform: translate(-7px, -3px); } }
  @keyframes hop { 50% { transform: translateY(-26px) rotate(-3deg); } }
  @keyframes nope { 25% { transform: rotate(-8deg); } 75% { transform: rotate(8deg); } }
  @keyframes rise { 50% { opacity: 0.4; transform: translateY(-5px); } }
</style>
