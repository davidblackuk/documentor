// pdf-view.js — standalone PDF viewer, opened from the dashboard's "PDF"
// button. Just an <iframe> onto the raw file endpoint plus a back link and
// theme toggle, so there's a way back without relying on the browser's
// native PDF viewer chrome.

import { initTheme } from "./theme.js";

const stem = new URLSearchParams(location.search).get("stem") || "";

document.getElementById("preview-page-title").textContent = stem ? `${stem}.pdf` : "";
document.title = stem ? `${stem} — PDF` : "DocuMentor — PDF";

initTheme();

if (stem) {
  document.getElementById("pdf-frame").src = `/api/pdf/${encodeURIComponent(stem)}/file`;
}
