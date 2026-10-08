/**
 * A tooltip that had to wrap keeps the full available width, which leaves a gap on the right when a word only just
 * wrapped. This narrows each tooltip to its longest line.
 */
function fit(el) {
  el.style.width = '';
  const range = document.createRange();
  range.selectNodeContents(el);
  const rects = [...range.getClientRects()].filter((r) => r.width > 0);
  if (rects.length < 2) return; // one line: already as narrow as it gets
  const style = getComputedStyle(el);
  const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const text = Math.max(...rects.map((r) => r.right)) - Math.min(...rects.map((r) => r.left));
  el.style.width = `${Math.ceil(text + padding) + 1}px`;
}

/** Watch for tooltips being added to the page; returns a function that stops watching. */
export function fitTooltips() {
  const observer = new MutationObserver((mutations) => {
    for (const { addedNodes } of mutations) {
      for (const node of addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        const tips = node.matches('.tooltip-content') ? [node] : [...node.querySelectorAll('.tooltip-content')];
        for (const tip of tips) requestAnimationFrame(() => fit(tip));
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
  return () => observer.disconnect();
}
