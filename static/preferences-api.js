// preferences-api.js — thin client for server-side UI preferences (theme,
// editor pane order, per-document font sizes and last-read page). Shared by
// app.js, theme.js, pdf-view.js and preview.js so each doesn't need its own
// copy of the fetch plumbing.

export async function getPreferences() {
  const r = await fetch("/api/preferences");
  if (!r.ok) throw new Error(`GET /api/preferences → ${r.status}`);
  return r.json();
}

export async function setTheme(theme) {
  return _put("/api/preferences/theme", { theme });
}

export async function setPaneOrder(order) {
  return _put("/api/preferences/pane-order", { order });
}

/** fields: any of { page, editorFont, previewFont } — omitted keys are left untouched. */
export async function setDocumentPrefs(stem, fields) {
  return _put(`/api/preferences/document/${encodeURIComponent(stem)}`, fields);
}

async function _put(url, body) {
  const r = await fetch(url, {
    method:  "PUT",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`PUT ${url} → ${r.status}`);
  return r.json();
}
