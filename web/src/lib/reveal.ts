/** Svelte action: fades a section in as it scrolls into view. */
export function reveal(node: HTMLElement) {
  node.classList.add("reveal");
  const io = new IntersectionObserver(
    ([e]) => {
      if (e.isIntersecting) {
        node.classList.add("in");
        io.disconnect();
      }
    },
    { threshold: 0.12 },
  );
  io.observe(node);
  return { destroy: () => io.disconnect() };
}
