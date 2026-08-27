/* Home-screen ("standalone") app-shell behaviour. Loaded on every page.
 *
 * Two jobs:
 *
 * 1. Mark the document as standalone so style.css can pad for the status bar
 *    and the home indicator. The (display-mode: standalone) media query
 *    handles this on its own in current browsers; navigator.standalone is the
 *    fallback for iOS home-screen apps that don't report display-mode.
 *
 * 2. Pin the chat shell to the visual viewport. In an iOS standalone window
 *    the layout viewport does not shrink when the on-screen keyboard opens, so
 *    a bottom-anchored composer ends up underneath the keyboard. Sizing the
 *    shell to the visual viewport instead moves it back above it.
 *
 * Job 2 is deliberately limited to pages carrying .app-shell (the chat page).
 * The other pages scroll their body normally, and the scroll correction below
 * would fight the user on them.
 */
(function () {
  const root = document.documentElement;
  const query = window.matchMedia
    ? window.matchMedia("(display-mode: standalone)")
    : null;

  let standalone = false;

  function applyMode() {
    standalone =
      window.navigator.standalone === true ||
      (query !== null && query.matches);
    root.dataset.standalone = standalone ? "1" : "0";
    if (!standalone) root.style.removeProperty("--app-height");
    return standalone;
  }

  applyMode();

  // Chrome can move a page between a tab and an installed window without a
  // reload; iOS can't, but the listener costs nothing.
  if (query !== null && query.addEventListener) {
    query.addEventListener("change", () => {
      applyMode();
      syncHeight();
    });
  }

  const viewport = window.visualViewport;
  const shell = document.body.classList.contains("app-shell");

  function syncHeight() {
    if (!shell || !viewport) return;
    if (!standalone) {
      root.style.removeProperty("--app-height");
      return;
    }
    // Floor it: a fractional height leaves a hairline of the keyboard visible
    // at the bottom of the composer.
    root.style.setProperty("--app-height", Math.floor(viewport.height) + "px");
  }

  if (!shell || !viewport) return;

  syncHeight();
  viewport.addEventListener("resize", syncHeight);

  // iOS scrolls the layout viewport when the keyboard opens rather than
  // resizing it, which pushes the header off the top of a fixed-height shell.
  // Nothing scrolls the window on this page legitimately — the transcript and
  // sidebar scroll themselves — so any non-zero offset here is the keyboard's.
  viewport.addEventListener("scroll", () => {
    if (standalone && window.scrollY !== 0) window.scrollTo(0, 0);
  });
})();
