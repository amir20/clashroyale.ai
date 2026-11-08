<script lang="ts">
  type Side = { label: string; value: number | null };
  let { title, a, b, digits = 0 }: { title: string; a: Side; b: Side; digits?: number } = $props();
  const show = (s: Side) => (s.value == null ? "—" : s.value.toFixed(digits));
</script>

<div class="flex items-center justify-between gap-4 py-3.5">
  <span class="text-sm text-base-content/70">{title}</span>
  <span class="flex shrink-0 gap-5 text-right tabular-nums">
    <span><span class="font-semibold text-primary">{show(a)}</span> <span class="text-xs text-base-content/60">{a.label}</span></span>
    <span><span class="font-semibold">{show(b)}</span> <span class="text-xs text-base-content/60">{b.label}</span></span>
  </span>
</div>
