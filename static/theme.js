// theme.js — light/dark toggle shared by the main app and the standalone
// markdown preview page. Toggles a data-theme attribute on <html>, persists
// the choice server-side, and swaps the highlight.js stylesheet to match.

import { getPreferences, setTheme } from "./preferences-api.js";

const HLJS_THEME_URL = {
  dark:  "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css",
  light: "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github.min.css",
};

/**
 * Wires up every .btn-theme-toggle button on the page and applies the
 * persisted (or default dark) theme once it's loaded from the server.
 * The page shows the CSS default (dark) until then — see :root in
 * style.css — so a saved "light" preference has a brief flash on load.
 *
 * Dispatches a "documenter-theme-change" event on window on every change
 * (including the initial apply) so callers that need to react — e.g.
 * EditorView syncing Monaco's separate theme system — can listen for it.
 */
export async function initTheme() {
  const hljsLink = document.getElementById("hljs-theme-link");
  const buttons  = document.querySelectorAll(".btn-theme-toggle");
  let theme = "dark";

  try {
    const prefs = await getPreferences();
    if (prefs.theme === "light") theme = "light";
  } catch {
    /* server unreachable — fall back to the CSS default */
  }

  function apply() {
    document.documentElement.dataset.theme = theme;
    if (hljsLink) hljsLink.href = HLJS_THEME_URL[theme];
    // Button shows the icon for the mode a click will switch *to*.
    const icon = theme === "dark" ? "☀️" : "🌙";
    for (const btn of buttons) btn.textContent = icon;
    window.dispatchEvent(new CustomEvent("documenter-theme-change", { detail: theme }));
  }

  apply();
  for (const btn of buttons) {
    btn.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      setTheme(theme).catch(console.error);
      apply();
    });
  }
}
