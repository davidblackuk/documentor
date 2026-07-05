// preview.js — standalone read-only markdown preview, opened from the
// dashboard's "Markdown" button. Deliberately independent of app.js/EditorView
// so it can be opened in its own tab without loading Monaco/PDF.js/Split.js.

import { initTheme } from "./theme.js";

const stem = new URLSearchParams(location.search).get("stem") || "";

const titleEl = document.getElementById("preview-page-title");
const bodyEl  = document.getElementById("preview-body");

titleEl.textContent = stem ? `${stem}.pdf` : "";
document.title = stem ? `${stem} — Preview` : "DocuMentor — Preview";

initTheme();

async function load() {
  if (!stem) {
    bodyEl.innerHTML = '<div class="loading-placeholder">No document specified.</div>';
    return;
  }
  try {
    const res = await fetch(`/api/pdf/${encodeURIComponent(stem)}/content`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { content } = await res.json();
    render(content);
  } catch (err) {
    bodyEl.innerHTML = `<div class="loading-placeholder">Failed to load: ${err}</div>`;
  }
}

function render(markdownText) {
  // marked.js and highlight.js are loaded globally from the CDN script tags.
  bodyEl.innerHTML = marked.parse(markdownText, { breaks: false });
  bodyEl.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));

  // Rewrite relative image paths, same as PreviewPane in app.js.
  const base = `/api/pdf/${encodeURIComponent(stem)}/images/`;
  bodyEl.querySelectorAll("img").forEach(img => {
    const src = img.getAttribute("src") ?? "";
    if (src.startsWith("images/"))       img.src = base + src.slice(7);
    else if (src.startsWith("/images/")) img.src = base + src.slice(8);
  });
}

load();
