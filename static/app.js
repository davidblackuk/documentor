/**
 * app.js — DocuMentor single-page application
 *
 * Architecture: thin classes with single responsibilities, wired together by
 * the top-level App instance.  No build step, no framework — just the browser.
 *
 * Class map
 * ---------
 *  Api              — all server communication (fetch + SSE)
 *  EventBus         — decoupled pub/sub so views don't reference each other
 *  DashboardView    — PDF list, model controls, scan progress
 *  PdfViewerPane    — PDF.js continuous-scroll wrapper
 *  MarkdownEditorPane — Monaco Editor wrapper
 *  PreviewPane      — live marked.js preview
 *  SyncScroller     — keeps all three panes at the same scroll percentage
 *  EditorView       — orchestrates the three panes and the toolbar
 *  App              — top-level router; owns both views
 */

"use strict";

import { initTheme } from "./theme.js";

/**
 * Distance from the top of `container`'s scrollable content to `el`, in the
 * container's own scroll coordinate space.
 *
 * `el.offsetTop` is NOT this — it's relative to `el.offsetParent`, which
 * (per spec) is the nearest *positioned* ancestor, not necessarily the
 * nearest scrolling one. In the three-pane editor layout, none of the pane
 * bodies are themselves positioned, so offsetTop resolves against
 * `.editor-panes` further up the tree, baking in a constant offset (the
 * pane header height, etc.) that made every sync-scroll target land short.
 * getBoundingClientRect() deltas are immune to that, since they're always
 * viewport-relative regardless of the positioned-ancestor chain.
 */
function offsetTopWithin(el, container) {
  return el.getBoundingClientRect().top - container.getBoundingClientRect().top
    + container.scrollTop;
}

// ============================================================================
// SECTION 1: API CLIENT
// All server communication in one place. Routes are string constants to avoid
// URL typos scattered across the codebase.
// ============================================================================

class Api {
  // ── PDF list and content ──────────────────────────────────────────────────

  static async listPdfs() {
    return Api._get("/api/pdfs");
  }

  static async getMarkdown(stem) {
    const data = await Api._get(`/api/pdf/${encodeURIComponent(stem)}/content`);
    return data.content;
  }

  static async saveMarkdown(stem, content) {
    return Api._put(`/api/pdf/${encodeURIComponent(stem)}/content`, { content });
  }

  /** Returns a URL suitable for PDF.js to load directly. */
  static pdfUrl(stem) {
    return `/api/pdf/${encodeURIComponent(stem)}/file`;
  }

  static async getPageCount(stem) {
    const data = await Api._get(`/api/pdf/${encodeURIComponent(stem)}/page-count`);
    return data.count;
  }

  // ── Code formatting ───────────────────────────────────────────────────────

  /** Unlike Api._post, surfaces the server's {detail} message on failure
   *  (e.g. "astyle is not installed…") instead of a bare status code. */
  static async formatCode(code, language) {
    const r = await fetch("/api/format-code", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ code, language }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      throw new Error(body?.detail || `Format request failed: ${r.status}`);
    }
    return r.json();
  }

  // ── Model ─────────────────────────────────────────────────────────────────

  static async getModelStatus() {
    return Api._get("/api/model");
  }

  /**
   * Open an SSE stream that fires onEvent(data) for each progress message
   * and onDone() when the stream ends.  Returns the EventSource so the
   * caller can close it early if needed.
   */
  static loadModelStream(onEvent, onDone) {
    return Api._sseStream("/api/model/load", onEvent, onDone);
  }

  static async unloadModel() {
    return Api._post("/api/model/unload");
  }

  // ── Output backup ─────────────────────────────────────────────────────────

  /** Unlike Api._post, surfaces the server's {detail} message on failure
   *  (e.g. a git push rejection) instead of a bare status code. */
  static async saveOutputPoint() {
    const r = await fetch("/api/output/save-point", { method: "POST" });
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      throw new Error(body?.detail || `Save point failed: ${r.status}`);
    }
    return r.json();
  }

  // ── Scanner ───────────────────────────────────────────────────────────────

  /**
   * Start a full scan via SSE.  pageEnd=0 means "scan to the last page".
   * Returns an EventSource — caller must close it when done.
   */
  static scanPdfStream(stem, pageStart, pageEnd, onEvent, onDone) {
    const params = new URLSearchParams({
      page_start: pageStart,
      page_end:   pageEnd,
    });
    const url = `/api/pdf/${encodeURIComponent(stem)}/scan?${params}`;
    return Api._sseStream(url, onEvent, onDone);
  }

  /**
   * Rescan a single page via fetch + ReadableStream (not EventSource, because
   * we need to read the final response after the SSE messages end).
   * Calls onEvent per message, resolves when the stream closes.
   */
  static async rescanPageStream(stem, page, onEvent) {
    const url = `/api/pdf/${encodeURIComponent(stem)}/rescan/${page}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Rescan failed: ${resp.status}`);
    await Api._readSseBody(resp.body, onEvent);
  }

  // ── Internals ─────────────────────────────────────────────────────────────

  static async _get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`GET ${url} → ${r.status}`);
    return r.json();
  }

  static async _post(url, body = {}) {
    const r = await fetch(url, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`POST ${url} → ${r.status}`);
    return r.json();
  }

  static async _put(url, body = {}) {
    const r = await fetch(url, {
      method:  "PUT",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`PUT ${url} → ${r.status}`);
    return r.json();
  }

  /** Open an EventSource and map message/error events to callbacks. */
  static _sseStream(url, onEvent, onDone) {
    const es = new EventSource(url);

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        onEvent(data);
        // Terminal events signal the server has closed its end.
        if (data.type === "done" || data.type === "error") {
          es.close();
          onDone?.();
        }
      } catch (_) { /* ignore malformed frames */ }
    };

    es.onerror = () => {
      es.close();
      onDone?.();
    };

    return es;
  }

  /**
   * Read a ReadableStream of SSE data (used for POST-based SSE via fetch).
   * Parses the "data: {...}" lines and calls onEvent for each JSON object.
   */
  static async _readSseBody(body, onEvent) {
    const reader  = body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // keep the incomplete last line

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try { onEvent(JSON.parse(line.slice(6))); } catch (_) {}
        }
      }
    }
  }
}


// ============================================================================
// SECTION 2: EVENT BUS
// Simple pub/sub so DashboardView and EditorView can communicate without
// holding direct references to each other. Keeps coupling at zero.
// ============================================================================

class EventBus {
  constructor() {
    this._listeners = {};
  }

  on(event, fn) {
    (this._listeners[event] ??= []).push(fn);
  }

  emit(event, ...args) {
    (this._listeners[event] ?? []).forEach(fn => fn(...args));
  }
}


// ============================================================================
// SECTION 3: DASHBOARD VIEW
// Renders the PDF list, handles model load/unload, and streams scan progress.
// ============================================================================

class DashboardView {
  /**
   * @param {EventBus} bus — emits "open-editor" when the user clicks Edit
   */
  constructor(bus) {
    this._bus = bus;

    // DOM references
    this._listEl       = document.getElementById("pdf-list");
    this._statusLabel  = document.getElementById("model-status-label");
    this._btnLoad      = document.getElementById("btn-load-model");
    this._btnUnload    = document.getElementById("btn-unload-model");
    this._btnScan      = document.getElementById("btn-scan-selected");
    this._progressPanel= document.getElementById("scan-progress-panel");
    this._progFilename = document.getElementById("progress-filename");
    this._progFraction = document.getElementById("progress-fraction");
    this._progFill     = document.getElementById("progress-bar-fill");
    this._logEl        = document.getElementById("scan-log");
    this._btnSavePoint = document.getElementById("btn-save-point");
    this._savePointStatus = document.getElementById("save-point-status");

    this._btnLoad.addEventListener("click",   () => this._loadModel());
    this._btnUnload.addEventListener("click", () => this._unloadModel());
    this._btnScan.addEventListener("click",   () => this._scanSelected());
    this._btnSavePoint.addEventListener("click", () => this._saveOutputPoint());
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  async show() {
    await Promise.all([this._refreshModelStatus(), this._refreshList()]);
  }

  // ── Model controls ────────────────────────────────────────────────────────

  async _refreshModelStatus() {
    try {
      const { loaded, name } = await Api.getModelStatus();
      this._setModelUI(loaded, name);
    } catch {
      this._statusLabel.textContent = "Model: unknown";
    }
  }

  _setModelUI(loaded, name) {
    this._statusLabel.textContent = loaded
      ? `Model: ${name} ✓`
      : `Model: ${name} (not loaded)`;
    this._statusLabel.className = `model-status-label ${loaded ? "loaded" : "unloaded"}`;
    this._btnLoad.disabled   =  loaded;
    this._btnUnload.disabled = !loaded;
  }

  _loadModel() {
    this._btnLoad.disabled = true;
    this._statusLabel.textContent = "Loading model…";
    this._appendLog("Starting model load…");

    Api.loadModelStream(
      (data) => this._appendLog(data.message),
      ()     => this._refreshModelStatus(),
    );
  }

  async _unloadModel() {
    this._btnUnload.disabled = true;
    await Api.unloadModel();
    await this._refreshModelStatus();
  }

  // ── Output backup ─────────────────────────────────────────────────────────

  async _saveOutputPoint() {
    this._btnSavePoint.disabled = true;
    this._savePointStatus.textContent = "Saving…";
    this._savePointStatus.className = "save-status dirty";
    try {
      const { committed } = await Api.saveOutputPoint();
      this._savePointStatus.textContent = committed ? "Saved ✓" : "Nothing to save";
      this._savePointStatus.className = "save-status saved";
    } catch (err) {
      this._savePointStatus.textContent = err.message;
      this._savePointStatus.className = "save-status error";
    } finally {
      this._btnSavePoint.disabled = false;
    }
  }

  // ── PDF list ──────────────────────────────────────────────────────────────

  async _refreshList() {
    this._listEl.innerHTML = '<div class="loading-placeholder">Loading…</div>';
    try {
      const pdfs = await Api.listPdfs();
      this._renderList(pdfs);
    } catch {
      this._listEl.innerHTML = '<div class="loading-placeholder">Failed to load PDF list.</div>';
    }
  }

  _renderList(pdfs) {
    if (!pdfs.length) {
      this._listEl.innerHTML =
        '<div class="loading-placeholder">No PDFs found in input/</div>';
      return;
    }

    this._listEl.innerHTML = pdfs.map(p => `
      <div class="pdf-row" data-stem="${p.stem}">
        <input class="pdf-checkbox" type="checkbox" data-stem="${p.stem}" />
        <span class="pdf-name" title="${p.filename}">${p.filename}</span>
        <span class="pdf-pages">${p.page_count} pp</span>
        <span class="status-badge status-${p.status}">${p.status}</span>
        <a class="btn btn-secondary btn-edit"
           href="/pdf-view.html?stem=${encodeURIComponent(p.stem)}">PDF</a>
        ${p.status === "scanned" ? `
          <a class="btn btn-secondary btn-edit"
             href="/preview.html?stem=${encodeURIComponent(p.stem)}">Markdown</a>
        ` : ""}
        <button class="btn btn-secondary btn-edit" data-stem="${p.stem}">Edit</button>
      </div>
    `).join("");

    // Edit buttons
    this._listEl.querySelectorAll(".btn-edit").forEach(btn => {
      btn.addEventListener("click", () => {
        this._bus.emit("open-editor", btn.dataset.stem);
      });
    });

    // Checkbox → enable/disable "Scan Selected"
    this._listEl.querySelectorAll(".pdf-checkbox").forEach(cb => {
      cb.addEventListener("change", () => this._updateScanButton());
    });
  }

  _updateScanButton() {
    const anyChecked = !!this._listEl.querySelector(".pdf-checkbox:checked");
    this._btnScan.disabled = !anyChecked;
  }

  _checkedStems() {
    return Array.from(
      this._listEl.querySelectorAll(".pdf-checkbox:checked"),
      el => el.dataset.stem,
    );
  }

  // ── Scan ──────────────────────────────────────────────────────────────────

  async _scanSelected() {
    const stems = this._checkedStems();
    if (!stems.length) return;

    this._btnScan.disabled = true;
    this._showProgressPanel();

    for (const stem of stems) {
      await this._scanOne(stem);
    }

    this._hideProgressPanel();
    await this._refreshList();
    this._btnScan.disabled = false;
  }

  _scanOne(stem) {
    return new Promise(resolve => {
      this._progFilename.textContent = stem;
      this._progFraction.textContent = "";
      this._progFill.style.width = "0%";

      Api.scanPdfStream(
        stem, 1, 0,   // 0 = scan all pages
        (data) => {
          this._appendLog(data.message);
          if (data.type === "page" && data.total) {
            const pct = Math.round((data.page / data.total) * 100);
            this._progFill.style.width = `${pct}%`;
            this._progFraction.textContent = `${data.page} / ${data.total}`;
          }
        },
        resolve,
      );
    });
  }

  // ── Progress panel helpers ─────────────────────────────────────────────────

  _showProgressPanel() {
    this._progressPanel.classList.remove("hidden");
    this._logEl.textContent = "";
  }

  _hideProgressPanel() {
    this._progressPanel.classList.add("hidden");
  }

  _appendLog(message) {
    if (!message) return;
    const line = document.createElement("div");
    line.textContent = message;
    this._logEl.appendChild(line);
    this._logEl.scrollTop = this._logEl.scrollHeight;
  }
}


// ============================================================================
// SECTION 4: PDF VIEWER PANE
// Wraps PDF.js in continuous-scroll mode: all pages are rendered as stacked
// canvases inside a single scrollable div.  Pages are rendered lazily as they
// enter the viewport so large documents don't block the main thread on open.
// ============================================================================

class PdfViewerPane {
  /**
   * @param {HTMLElement} container — the scrollable .pane-body div
   * @param {HTMLElement} indicator — the "p.N/total" label in the pane header
   * @param {HTMLElement} [percentEl] — the "N%" scroll-position label, if present
   */
  constructor(container, indicator, percentEl = null) {
    this._container  = container;
    this._indicator  = indicator;
    this._percentEl  = percentEl;
    this._pdf        = null;      // loaded PDFDocumentProxy
    this._pageCount  = 0;
    this._rendered   = new Set(); // page numbers already drawn
    this._observer   = null;      // IntersectionObserver for lazy rendering
  }

  // ── Loading ───────────────────────────────────────────────────────────────

  async load(pdfUrl) {
    // PDF.js is loaded on first use via dynamic import so the heavy library
    // doesn't block the dashboard page load.
    const lib = await import(
      "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.2.67/build/pdf.min.mjs"
    );

    lib.GlobalWorkerOptions.workerSrc =
      "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.2.67/build/pdf.worker.min.mjs";

    this._pdf       = await lib.getDocument(pdfUrl).promise;
    this._pageCount = this._pdf.numPages;
    this._buildPageSkeleton();
    this._startLazyObserver();
  }

  /**
   * Create a placeholder wrapper div for every page.
   * The actual canvas is only rendered when the wrapper becomes visible.
   */
  _buildPageSkeleton() {
    this._container.innerHTML = "";
    this._rendered.clear();

    for (let i = 1; i <= this._pageCount; i++) {
      const wrapper = document.createElement("div");
      wrapper.className = "pdf-page-wrapper";
      wrapper.dataset.page = i;
      // Reserve approximate height so the scrollbar is accurate before render.
      wrapper.style.minHeight = "800px";
      this._container.appendChild(wrapper);
    }
  }

  /** Use IntersectionObserver to render pages only when they scroll into view. */
  _startLazyObserver() {
    if (this._observer) this._observer.disconnect();

    this._observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const page = parseInt(entry.target.dataset.page, 10);
          if (!this._rendered.has(page)) {
            this._renderPage(page, entry.target);
          }
        }
      },
      { root: this._container, rootMargin: "200px" }, // pre-render 200px ahead
    );

    this._container.querySelectorAll(".pdf-page-wrapper").forEach(el => {
      this._observer.observe(el);
    });

    // Scroll listener updates the page indicator as the user scrolls.
    this._container.addEventListener("scroll", () => this._updateIndicator(), { passive: true });
    this._updateIndicator(); // show p.1 / 0% immediately, before any scroll fires
  }

  async _renderPage(pageNum, wrapper) {
    this._rendered.add(pageNum);

    const page     = await this._pdf.getPage(pageNum);
    // Scale to fill the pane width at ~1.5× viewport ratio for readability.
    const viewport = page.getViewport({ scale: 1.5 });

    const canvas  = document.createElement("canvas");
    canvas.width  = viewport.width;
    canvas.height = viewport.height;

    // overflow:hidden on a flex-item wrapper collapses it to 0 height even
    // when content is present. Setting aspect-ratio gives the wrapper its
    // correct proportions before the canvas renders, keeping overflow:hidden
    // (needed for border-radius clipping) without the height collapse.
    wrapper.style.minHeight  = "";
    wrapper.style.aspectRatio = `${viewport.width} / ${viewport.height}`;
    wrapper.appendChild(canvas);

    await page.render({
      canvasContext: canvas.getContext("2d"),
      viewport,
    }).promise;
  }

  _updateIndicator() {
    const visible = this._visiblePageNumber();
    if (visible) {
      this._indicator.textContent = `p.${visible} / ${this._pageCount}`;
    }
    if (this._percentEl) {
      this._percentEl.textContent = `${this._scrollPercent()}%`;
    }
  }

  /** Percentage scrolled through the pane, 0 at the top and 100 at the bottom. */
  _scrollPercent() {
    const scrollable = this._container.scrollHeight - this._container.clientHeight;
    if (scrollable <= 0) return 100;
    return Math.round((this._container.scrollTop / scrollable) * 100);
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  /** Return the 1-indexed page number currently most visible in the viewport. */
  getVisiblePage() {
    return this._visiblePageNumber() ?? 1;
  }

  /** Scroll so that page N is flush with the top of the viewport. */
  setPage(n) {
    const wrapper = this._container.querySelector(`[data-page="${n}"]`);
    if (wrapper) this._container.scrollTop = offsetTopWithin(wrapper, this._container);
  }

  onScroll(fn) {
    this._container.addEventListener("scroll", fn, { passive: true });
  }

  /** Return the 1-indexed page number most visible in the viewport. */
  _visiblePageNumber() {
    const wrappers = this._container.querySelectorAll(".pdf-page-wrapper");
    const center   = this._container.scrollTop + this._container.clientHeight / 2;

    let closest = null;
    let minDist = Infinity;

    for (const el of wrappers) {
      const mid  = offsetTopWithin(el, this._container) + el.offsetHeight / 2;
      const dist = Math.abs(mid - center);
      if (dist < minDist) {
        minDist = dist;
        closest = parseInt(el.dataset.page, 10);
      }
    }
    return closest;
  }
}


// ============================================================================
// SECTION 5: MARKDOWN EDITOR PANE
// Monaco Editor wrapper.  Exposes a stable interface so SyncScroller and
// EditorView don't depend on Monaco's internal API surface.
// ============================================================================

class MarkdownEditorPane {
  /**
   * @param {HTMLElement} container — the .monaco-container div
   */
  constructor(container) {
    this._container = container;
    this._editor    = null;    // monaco.editor.IStandaloneCodeEditor
    this._ready     = false;
  }

  // ── Initialisation ────────────────────────────────────────────────────────

  /**
   * Load Monaco via its AMD loader and create the editor instance.
   * Returns a Promise that resolves once the editor is interactive.
   */
  init(initialContent = "") {
    return new Promise(resolve => {
      require.config({
        paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs" },
      });

      require(["vs/editor/editor.main"], () => {
        this._editor = monaco.editor.create(this._container, {
          value:         initialContent,
          language:      "markdown",
          theme:         document.documentElement.dataset.theme === "light" ? "vs" : "vs-dark",
          wordWrap:      "on",
          minimap:       { enabled: false },
          fontSize:      14,
          lineNumbers:   "on",
          scrollBeyondLastLine: false,
          // automaticLayout lets Monaco re-measure after Split.js resizes the pane.
          automaticLayout: true,
          padding:       { top: 12 },
        });

        // Surround selected text when * or _ is typed, instead of replacing.
        // Monaco's built-in autoSurround handles backticks; add * and _ here.
        // Keeping the wrapped content selected lets a second * press give **text**.
        this._editor.onKeyDown(e => {
          const ch = e.browserEvent.key;
          if (ch !== '*' && ch !== '_') return;
          const sel = this._editor.getSelection();
          if (!sel || sel.isEmpty()) return;

          e.preventDefault();
          e.stopPropagation();

          const text = this._editor.getModel().getValueInRange(sel);
          this._editor.executeEdits('md-surround', [{
            range: sel,
            text:  `${ch}${text}${ch}`,
            forceMoveMarkers: true,
          }]);

          // Re-select the wrapped content so pressing * again upgrades to **text**.
          // Single-line: end shifts by +2 (leading ch shifts col right, trailing ch extends).
          // Multi-line:  end shifts by +1 (only the trailing ch is on the end line).
          const sameLine = sel.startLineNumber === sel.endLineNumber;
          this._editor.setSelection(new monaco.Selection(
            sel.startLineNumber, sel.startColumn,
            sel.endLineNumber,   sel.endColumn + (sameLine ? 2 : 1),
          ));
        });

        this._ready = true;
        resolve();
      });
    });
  }

  // ── Content ───────────────────────────────────────────────────────────────

  getValue() {
    return this._editor?.getValue() ?? "";
  }

  setValue(content) {
    if (!this._editor) return;
    // pushEditOperations preserves the undo history, unlike setValue().
    this._editor.executeEdits("external", [{
      range: this._editor.getModel().getFullModelRange(),
      text:  content,
    }]);
  }

  /** Adjust the editor font size without rebuilding the editor instance. */
  setFontSize(px) {
    this._editor?.updateOptions({ fontSize: px });
  }

  /** monaco.editor.setTheme is global (not per-instance) and only exists once
   *  the AMD bundle has loaded, so this is a no-op until init() resolves. */
  setTheme(theme) {
    if (!this._ready) return;
    monaco.editor.setTheme(theme === "light" ? "vs" : "vs-dark");
  }

  /**
   * Listen for content changes.  Returns a disposable (call .dispose() to stop).
   */
  onChange(fn) {
    return this._editor?.onDidChangeModelContent(fn);
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  /**
   * Return the <!-- page N --> number at the top of the visible Monaco viewport.
   * Scans backwards from the first visible line to find the nearest page marker.
   */
  getVisiblePage() {
    if (!this._editor) return 1;
    const model  = this._editor.getModel();
    const ranges = this._editor.getVisibleRanges();
    if (!ranges.length) return 1;
    const topLine = ranges[0].startLineNumber;
    for (let ln = topLine; ln >= 1; ln--) {
      const m = model.getLineContent(ln).match(/<!--\s*page\s+(\d+)\s*-->/i);
      if (m) return parseInt(m[1], 10);
    }
    return 1;
  }

  /** Scroll Monaco so that the <!-- page N --> marker is at the top of the viewport. */
  setPage(n) {
    if (!this._editor) return;
    const model = this._editor.getModel();
    const re    = new RegExp(`<!--\\s*page\\s+${n}\\s*-->`, 'i');
    for (let ln = 1, count = model.getLineCount(); ln <= count; ln++) {
      if (re.test(model.getLineContent(ln))) {
        // getTopForLineNumber gives the y-offset of the line; setScrollTop is immediate.
        this._editor.setScrollTop(this._editor.getTopForLineNumber(ln));
        return;
      }
    }
  }

  onScroll(fn) {
    return this._editor?.onDidScrollChange(fn);
  }

  // ── Code block wrapping ─────────────────────────────────────────────────────

  hasSelection() {
    const sel = this._editor?.getSelection();
    return !!sel && !sel.isEmpty();
  }

  getSelectedText() {
    const sel = this._editor.getSelection();
    return this._editor.getModel().getValueInRange(sel);
  }

  /**
   * True if the current selection is already a fenced code block, or sits
   * directly inside one (fence lines immediately before/after it).
   */
  isSelectionAlreadyFenced() {
    const sel = this._editor?.getSelection();
    if (!sel || sel.isEmpty()) return false;
    const model = this._editor.getModel();

    const text = model.getValueInRange(sel).trim();
    if (text.length > 3 && text.startsWith("```") && text.endsWith("```")) return true;

    const before = sel.startLineNumber > 1
      ? model.getLineContent(sel.startLineNumber - 1).trim() : "";
    const after = sel.endLineNumber < model.getLineCount()
      ? model.getLineContent(sel.endLineNumber + 1).trim() : "";
    return before.startsWith("```") && after.startsWith("```");
  }

  /** Replace the current selection with a ```language fenced block. */
  wrapSelectionInFence(language, text) {
    const sel = this._editor.getSelection();
    const fenced = "```" + language + "\n" + text.replace(/\n+$/, "") + "\n```";
    this._editor.executeEdits("wrap-code-fence", [{
      range: sel,
      text:  fenced,
      forceMoveMarkers: true,
    }]);
    this._editor.focus();
  }

  // ── Page detection ────────────────────────────────────────────────────────

  /**
   * Return the <!-- page N --> number at the current cursor position, or null.
   * Used by the Rescan button to know which page to re-OCR.
   */
  getCurrentPageNumber() {
    if (!this._editor) return null;

    const model     = this._editor.getModel();
    const cursorPos = this._editor.getPosition();
    if (!cursorPos) return null;

    // Walk backwards from the cursor line to find the nearest page marker.
    for (let line = cursorPos.lineNumber; line >= 1; line--) {
      const text  = model.getLineContent(line);
      const match = text.match(/<!--\s*page\s+(\d+)\s*-->/i);
      if (match) return parseInt(match[1], 10);
    }
    return null;
  }
}


// ============================================================================
// SECTION 6: PREVIEW PANE
// Renders Markdown as HTML using marked.js.  The preview is regenerated on
// every editor change (debounced to avoid thrashing the DOM).
// ============================================================================

class PreviewPane {
  /**
   * @param {HTMLElement} bodyEl — the scrollable .preview-body div
   */
  constructor(bodyEl) {
    this._body         = bodyEl;
    this._stem         = null;   // set by EditorView so image URLs can be resolved
    this._debounceTimer = null;
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  /**
   * Schedule a re-render after 150 ms of inactivity.
   * Debouncing prevents the DOM from being replaced on every keypress.
   */
  update(markdownText, stem = null) {
    if (stem) this._stem = stem;
    clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => this._render(markdownText), 150);
  }

  _render(markdownText) {
    // Insert <a id="pg{N}"> anchors before each <!-- page N --> marker so that
    // getVisiblePage() and setPage() can find page boundaries regardless of
    // whether the OCR output already contains <a id="page-N"> anchors.
    const processed = markdownText.replace(
      /<!--\s*page\s+(\d+)\s*-->/g,
      (_, n) => `<a id="pg${n}" class="page-anchor"></a>`,
    );

    // marked.js is loaded globally from the CDN script tag in index.html.
    this._body.innerHTML = marked.parse(processed, { breaks: false });

    // Syntax-highlight every fenced code block.  hljs.highlightElement() is
    // idempotent so re-renders are safe.
    this._body.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));

    // Rewrite relative image paths to the API endpoint so the browser can
    // find extracted figures stored in output/{stem}/images/.
    if (this._stem) {
      const base = `/api/pdf/${encodeURIComponent(this._stem)}/images/`;
      this._body.querySelectorAll("img").forEach(img => {
        const src = img.getAttribute("src") ?? "";
        // Handle both relative ("images/foo.jpg") and absolute ("/images/foo.jpg") paths.
        if (src.startsWith("images/"))  img.src = base + src.slice(7);
        else if (src.startsWith("/images/")) img.src = base + src.slice(8);
      });
    }
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  /** Return the page number whose anchor is at or just above the current scroll top. */
  getVisiblePage() {
    const top  = this._body.scrollTop + 10;
    let   page = 1;
    this._body.querySelectorAll(".page-anchor[id^='pg']").forEach(el => {
      const n = parseInt(el.id.slice(2), 10);
      if (!isNaN(n) && offsetTopWithin(el, this._body) <= top) page = n;
    });
    return page;
  }

  /** Scroll preview so that the page N anchor is at the top of the viewport. */
  setPage(n) {
    const anchor = this._body.querySelector(`[id="pg${n}"]`);
    if (anchor) this._body.scrollTop = offsetTopWithin(anchor, this._body);
  }

  onScroll(fn) {
    this._body.addEventListener("scroll", fn, { passive: true });
  }
}


// ============================================================================
// SECTION 7: SYNC SCROLL CONTROLLER
// Keeps all three panes showing the same page number.
//
// Page-based sync: each pane reports which page is at the top of its viewport
// via getVisiblePage(), and scrolls to a target page via setPage().  This is
// far more accurate than percentage-based sync because the three panes have
// different scroll heights (Monaco line height ≠ PDF page height ≠ preview
// line height).
//
// Mutual exclusion: when we programmatically scroll pane B in response to
// pane A scrolling, B fires its own scroll event.  The _locked flag prevents
// that from triggering another round of updates.
// ============================================================================

class SyncScroller {
  /**
   * @param {MarkdownEditorPane} editor
   * @param {PreviewPane}        preview
   * @param {PdfViewerPane}      pdf
   */
  constructor(editor, preview, pdf) {
    this._editor  = editor;
    this._preview = preview;
    this._pdf     = pdf;
    this._enabled = true;
    this._locked  = false;

    this._attach();
  }

  enable()  { this._enabled = true;  }
  disable() { this._enabled = false; }

  // ── Internal ──────────────────────────────────────────────────────────────

  _attach() {
    this._editor.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("editor", this._editor.getVisiblePage());
    });

    this._preview.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("preview", this._preview.getVisiblePage());
    });

    this._pdf.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("pdf", this._pdf.getVisiblePage());
    });
  }

  /**
   * Scroll the other two panes to show the same page as the source pane.
   * requestAnimationFrame defers the lock release until after the browser
   * has processed the programmatic scroll events we're about to trigger.
   */
  _sync(source, page) {
    if (!page) return;
    this._locked = true;

    if (source !== "editor")  this._editor.setPage(page);
    if (source !== "preview") this._preview.setPage(page);
    if (source !== "pdf")     this._pdf.setPage(page);

    requestAnimationFrame(() => { this._locked = false; });
  }
}


// ============================================================================
// SECTION 8: EDITOR VIEW
// Orchestrates the three panes, the Split.js dividers, the toolbar buttons,
// and the auto-save / rescan flows.
// ============================================================================

const PANE_IDS = ["pane-editor", "pane-preview", "pane-pdf"];

class EditorView {
  /**
   * @param {EventBus} bus — listens for "open-editor", emits "back-to-dashboard"
   */
  constructor(bus) {
    this._bus  = bus;
    this._stem = null;

    // Toolbar elements
    this._titleEl      = document.getElementById("editor-title");
    this._saveStatusEl = document.getElementById("save-status");
    this._btnBack      = document.getElementById("btn-back");
    this._btnSave      = document.getElementById("btn-save");
    this._btnRescan    = document.getElementById("btn-rescan-page");
    this._btnCodeBlock = document.getElementById("btn-code-block");
    this._codeLangMenu = document.getElementById("code-lang-menu");
    this._syncToggle        = document.getElementById("toggle-sync-scroll");
    this._editorFontSelect  = document.getElementById("editor-font-size");
    this._previewFontSelect = document.getElementById("preview-font-size");
    this._overlay      = document.getElementById("rescan-overlay");
    this._overlayMsg   = document.getElementById("rescan-message");
    this._unsavedOverlay = document.getElementById("unsaved-overlay");
    this._btnUnsavedCancel  = document.getElementById("btn-unsaved-cancel");
    this._btnUnsavedDiscard = document.getElementById("btn-unsaved-discard");
    this._btnUnsavedSave    = document.getElementById("btn-unsaved-save");

    // Pane elements
    this._monacoContainer = document.getElementById("monaco-container");
    this._previewBody     = document.getElementById("preview-body");
    this._pdfBody         = document.getElementById("pdf-body");
    this._pdfIndicator    = document.getElementById("pdf-page-indicator");
    this._pdfPercent      = document.getElementById("pdf-scroll-percent");

    // Component instances — created once, reused across documents
    this._editorPane  = new MarkdownEditorPane(this._monacoContainer);
    this._previewPane = new PreviewPane(this._previewBody);
    this._pdfPane     = new PdfViewerPane(this._pdfBody, this._pdfIndicator, this._pdfPercent);
    this._scroller    = null;   // created after Monaco is ready

    this._dirty          = false;
    this._baseTitle      = document.title; // restored when the star is cleared
    this._monacoReady    = false;
    this._splitInstance  = null;
    this._savePageTimer  = null;

    // Pane order — a layout preference, not per-document, so it's loaded
    // and applied once here rather than in open().
    this._panesContainer = document.getElementById("editor-panes");
    this._paneOrder = this._loadPaneOrder();
    this._applyPaneOrder();

    this._wireToolbar();
    this._wirePaneMoveButtons();

    // Monaco's theme is set at creation time from the current preference
    // (see MarkdownEditorPane.init); this keeps it in sync on live toggles.
    window.addEventListener("documenter-theme-change", (e) => this._editorPane.setTheme(e.detail));
  }

  // ── Per-document persistence (localStorage) ──────────────────────────────

  _saveLastPage(page) {
    if (this._stem && page) localStorage.setItem(`documenter.page.${this._stem}`, page);
  }

  _loadLastPage(stem) {
    return parseInt(localStorage.getItem(`documenter.page.${stem}`), 10) || 1;
  }

  _saveFontSizes(stem) {
    localStorage.setItem(`documenter.editorFont.${stem}`, this._editorFontSelect.value);
    localStorage.setItem(`documenter.previewFont.${stem}`, this._previewFontSelect.value);
  }

  _restoreFontSizes(stem) {
    const edPx   = parseInt(localStorage.getItem(`documenter.editorFont.${stem}`),  10);
    const prevPx = parseInt(localStorage.getItem(`documenter.previewFont.${stem}`), 10);

    if (edPx) {
      this._editorFontSelect.value = edPx;
      this._editorPane.setFontSize(edPx);
    }
    if (prevPx) {
      this._previewFontSelect.value = prevPx;
      this._previewBody.style.fontSize = `${prevPx}px`;
    }
  }

  // ── Pane order (localStorage) ─────────────────────────────────────────────

  _loadPaneOrder() {
    try {
      const stored = JSON.parse(localStorage.getItem("documenter.paneOrder"));
      if (Array.isArray(stored) &&
          stored.length === PANE_IDS.length &&
          PANE_IDS.every((id) => stored.includes(id))) {
        return stored;
      }
    } catch { /* malformed value — fall through to default */ }
    return [...PANE_IDS];
  }

  _savePaneOrder() {
    localStorage.setItem("documenter.paneOrder", JSON.stringify(this._paneOrder));
  }

  _applyPaneOrder() {
    for (const id of this._paneOrder) {
      this._panesContainer.appendChild(document.getElementById(id));
    }
  }

  _wirePaneMoveButtons() {
    for (const btn of document.querySelectorAll(".btn-pane-move")) {
      btn.addEventListener("click", () => this._movePane(btn.dataset.pane, btn.dataset.move));
    }
    this._updatePaneMoveButtons();
  }

  _updatePaneMoveButtons() {
    for (const btn of document.querySelectorAll(".btn-pane-move")) {
      const i = this._paneOrder.indexOf(btn.dataset.pane);
      btn.disabled = btn.dataset.move === "left" ? i === 0 : i === this._paneOrder.length - 1;
    }
  }

  _movePane(paneId, direction) {
    const i = this._paneOrder.indexOf(paneId);
    const j = direction === "left" ? i - 1 : i + 1;
    if (j < 0 || j >= this._paneOrder.length) return;

    // Sizes track DOM position, not pane identity — swap them alongside the
    // order so a pane keeps its own width as it moves.
    const sizes = this._splitInstance ? this._splitInstance.getSizes() : null;
    [this._paneOrder[i], this._paneOrder[j]] = [this._paneOrder[j], this._paneOrder[i]];
    if (sizes) [sizes[i], sizes[j]] = [sizes[j], sizes[i]];

    this._applyPaneOrder();
    this._savePaneOrder();
    this._recreateSplit(sizes);
    this._updatePaneMoveButtons();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  /**
   * Open a document in the editor.
   * Handles first-open Monaco initialisation and subsequent document swaps.
   */
  async open(stem) {
    this._stem  = stem;

    this._titleEl.textContent = `${stem}.pdf`;

    // ── Load markdown and PDF in parallel ─────────────────────────────────
    const [markdown] = await Promise.all([
      Api.getMarkdown(stem),
      this._pdfPane.load(Api.pdfUrl(stem)),
    ]);

    // ── Initialise Monaco on first open ───────────────────────────────────
    if (!this._monacoReady) {
      await this._editorPane.init(markdown);
      this._monacoReady = true;
      this._initSplit();
      this._initSyncScroll();
      this._wireEditorEvents();
    } else {
      this._editorPane.setValue(markdown);
    }

    // setValue() (and, on some Monaco versions, init()) fires the same
    // onDidChangeModelContent event a real user edit would, which the
    // dirty-tracking listener in _wireEditorEvents() can't tell apart from
    // an actual edit. Reset dirty state after loading, not before, so
    // opening/switching documents never starts the editor in a dirty state.
    this._setDirty(false);
    this._setSaveStatus("");

    // Render initial preview
    this._previewPane.update(markdown, stem);

    // Restore per-document font sizes (instant — no layout dependency).
    this._restoreFontSizes(stem);

    // Restore the last scroll position for this document.  The small delay
    // lets the PDF container finish its initial layout before we set scrollTop.
    const lastPage = this._loadLastPage(stem);
    if (lastPage > 1) {
      setTimeout(() => {
        this._pdfPane.setPage(lastPage);
        this._editorPane.setPage(lastPage);
        this._previewPane.setPage(lastPage);
      }, 300);
    }
  }

  // ── Split.js — resizable pane dividers ────────────────────────────────────

  _initSplit() {
    if (this._splitInstance) return;   // only create once
    this._recreateSplit([34, 33, 33]);
  }

  /** (Re)builds the Split.js instance for the current pane order. Used both
   *  for the first-open setup and whenever panes are reordered, since
   *  Split.js bakes DOM order into its gutters at construction time. */
  _recreateSplit(sizes) {
    if (this._splitInstance) this._splitInstance.destroy();

    this._splitInstance = Split(
      this._paneOrder.map((id) => `#${id}`),
      {
        sizes:     sizes || [34, 33, 33],
        minSize:   200,
        gutterSize: 6,
        direction: "horizontal",
        // Monaco reflows automatically via automaticLayout:true, but we need
        // to coerce the PDF container width after a drag ends.
        onDragEnd: () => window.dispatchEvent(new Event("resize")),
      },
    );
    window.dispatchEvent(new Event("resize"));
  }

  // ── Sync scroll ───────────────────────────────────────────────────────────

  _initSyncScroll() {
    this._scroller = new SyncScroller(
      this._editorPane,
      this._previewPane,
      this._pdfPane,
    );

    this._syncToggle.addEventListener("change", () => {
      this._syncToggle.checked
        ? this._scroller.enable()
        : this._scroller.disable();
    });
  }

  // ── Editor change / auto-preview ─────────────────────────────────────────

  _wireEditorEvents() {
    this._editorPane.onChange(() => {
      this._setDirty(true);
      this._setSaveStatus("Unsaved", "dirty");
      this._previewPane.update(this._editorPane.getValue());
    });

    // Persist the current page to localStorage 500 ms after scrolling stops.
    // The PDF pane is used as the single source of truth (sync scroll keeps
    // all three panes aligned, so any pane would give the same page number).
    this._pdfPane.onScroll(() => {
      clearTimeout(this._savePageTimer);
      this._savePageTimer = setTimeout(() => {
        this._saveLastPage(this._pdfPane.getVisiblePage());
      }, 500);
    });
  }

  // ── Toolbar ───────────────────────────────────────────────────────────────

  _wireToolbar() {
    this._btnBack.addEventListener("click", () => this._onBackClicked());

    this._btnUnsavedCancel.addEventListener("click", () => this._hideUnsavedPrompt());
    this._btnUnsavedDiscard.addEventListener("click", () => {
      this._hideUnsavedPrompt();
      this._setDirty(false);
      this._bus.emit("back-to-dashboard");
    });
    this._btnUnsavedSave.addEventListener("click", async () => {
      const saved = await this._save();
      if (!saved) return; // leave the prompt open so the user can retry or discard
      this._hideUnsavedPrompt();
      this._bus.emit("back-to-dashboard");
    });
    // Clicking the dimmed backdrop (not the dialog box itself) cancels, same as the Cancel button.
    this._unsavedOverlay.addEventListener("click", (e) => {
      if (e.target === this._unsavedOverlay) this._hideUnsavedPrompt();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this._unsavedOverlay.classList.contains("hidden")) {
        this._hideUnsavedPrompt();
      }
    });

    // The app never pushes browser history entries — view switches are just
    // in-page class toggles — so the Back/Forward buttons don't hit our own
    // "leave with unsaved changes?" prompt at all; they navigate the tab
    // itself away from the app. beforeunload is the only hook that covers
    // that (and also tab close / reload / typing a new URL) with a native
    // browser-level confirmation. Custom text in returnValue is ignored by
    // modern browsers, which always show their own generic wording — the
    // assignment is just what triggers the dialog to appear at all.
    window.addEventListener("beforeunload", (e) => {
      if (!this._dirty) return;
      e.preventDefault();
      e.returnValue = "";
    });

    this._btnSave.addEventListener("click", () => this._save());

    this._btnRescan.addEventListener("click", () => this._rescanCurrentPage());

    this._btnCodeBlock.addEventListener("click", () => this._onCodeBlockClick());
    this._codeLangMenu.querySelectorAll(".code-lang-option").forEach(btn => {
      btn.addEventListener("click", () => {
        this._codeLangMenu.classList.add("hidden");
        this._wrapSelectionAsCode(btn.dataset.lang);
      });
    });
    document.addEventListener("click", (e) => {
      if (e.target !== this._btnCodeBlock && !this._codeLangMenu.contains(e.target)) {
        this._codeLangMenu.classList.add("hidden");
      }
    });

    this._editorFontSelect.addEventListener("change", () => {
      this._editorPane.setFontSize(parseInt(this._editorFontSelect.value, 10));
      this._saveFontSizes(this._stem);
    });

    this._previewFontSelect.addEventListener("change", () => {
      this._previewBody.style.fontSize = `${this._previewFontSelect.value}px`;
      this._saveFontSizes(this._stem);
    });

    // Ctrl+S / Cmd+S to save
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        this._save();
      }
    });

    // Ctrl+Shift+C / Cmd+Shift+C — wrap selection as C.
    // Ctrl+Shift+B / Cmd+Shift+B — wrap selection as BASIC.
    // Note: Ctrl+Shift+C is also Chrome DevTools' inspect-element shortcut;
    // since that's a browser-level binding, it may win over this handler
    // depending on browser/OS.
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || !e.shiftKey) return;
      const key = e.key.toLowerCase();
      if (key === "c") {
        e.preventDefault();
        this._onCodeShortcut("c");
      } else if (key === "b") {
        e.preventDefault();
        this._onCodeShortcut("basic");
      }
    });
  }

  /** Returns true on success, false on failure — callers that gate further
   *  actions (like leaving the editor) on a successful save check this. */
  async _save() {
    if (!this._stem) return false;
    this._setSaveStatus("Saving…");
    try {
      await Api.saveMarkdown(this._stem, this._editorPane.getValue());
      this._setDirty(false);
      this._setSaveStatus("Saved", "saved");
      setTimeout(() => this._setSaveStatus(""), 2000);
      return true;
    } catch {
      this._setSaveStatus("Save failed", "dirty");
      return false;
    }
  }

  // ── Unsaved-changes prompt (shown when leaving the editor with pending edits) ──

  _onBackClicked() {
    if (this._dirty) {
      this._unsavedOverlay.classList.remove("hidden");
    } else {
      this._bus.emit("back-to-dashboard");
    }
  }

  _hideUnsavedPrompt() {
    this._unsavedOverlay.classList.add("hidden");
  }

  _setSaveStatus(text, modifier = "") {
    this._saveStatusEl.textContent  = text;
    this._saveStatusEl.className    = `save-status ${modifier}`;
  }

  /** Tracks unsaved changes and mirrors them onto the tab title (a leading
   *  star) so they're visible even when this tab isn't focused. */
  _setDirty(value) {
    this._dirty = value;
    document.title = value ? `★ ${this._baseTitle}` : this._baseTitle;
  }

  // ── Code block wrapping ──────────────────────────────────────────────────

  _onCodeBlockClick() {
    if (!this._canWrapSelection()) return;
    this._codeLangMenu.classList.toggle("hidden");
  }

  /** Ctrl/Cmd+Shift+C and +B — wrap the selection directly in the given
   *  language, skipping the { } Code dropdown. */
  _onCodeShortcut(language) {
    if (!this._canWrapSelection()) return;
    this._codeLangMenu.classList.add("hidden");
    this._wrapSelectionAsCode(language);
  }

  _canWrapSelection() {
    if (!this._editorPane.hasSelection()) {
      alert("Select the code you want to wrap first.");
      return false;
    }
    // Already fenced — leave it alone rather than double-wrapping.
    if (this._editorPane.isSelectionAlreadyFenced()) return false;
    return true;
  }

  async _wrapSelectionAsCode(language) {
    const original = this._editorPane.getSelectedText();
    let text = original;
    try {
      const { formatted } = await Api.formatCode(original, language);
      text = formatted;
    } catch (err) {
      alert(`Couldn't format the code, wrapping it as-is.\n\n${err.message}`);
    }
    this._editorPane.wrapSelectionInFence(language, text);
    this._setDirty(true);
    this._setSaveStatus("Unsaved", "dirty");
  }

  // ── Rescan current page ───────────────────────────────────────────────────

  async _rescanCurrentPage() {
    if (!this._stem) return;

    // Find the page number from the cursor position in the editor.
    const page = this._editorPane.getCurrentPageNumber();
    if (!page) {
      alert("Could not determine the current page — place the cursor inside a page section.");
      return;
    }

    this._showRescanOverlay(`Rescanning page ${page}…`);
    this._btnRescan.disabled = true;

    try {
      await Api.rescanPageStream(this._stem, page, (data) => {
        this._overlayMsg.textContent = data.message ?? `Rescanning page ${page}…`;
      });

      // Reload the updated markdown from disk and refresh all panes.
      const updated = await Api.getMarkdown(this._stem);
      this._editorPane.setValue(updated);
      this._previewPane.update(updated);
      this._setDirty(false);
      this._setSaveStatus("Saved", "saved");
      setTimeout(() => this._setSaveStatus(""), 2000);

    } catch (err) {
      alert(`Rescan failed: ${err.message}`);
    } finally {
      this._hideRescanOverlay();
      this._btnRescan.disabled = false;
    }
  }

  _showRescanOverlay(message) {
    this._overlayMsg.textContent = message;
    this._overlay.classList.remove("hidden");
  }

  _hideRescanOverlay() {
    this._overlay.classList.add("hidden");
  }
}


// ============================================================================
// SECTION 9: APPLICATION
// Top-level router: owns both views and switches between them via CSS classes.
// Wires the EventBus so views communicate without direct references.
// ============================================================================

class App {
  constructor() {
    this._bus      = new EventBus();
    this._dashView = new DashboardView(this._bus);
    this._editView = new EditorView(this._bus);

    this._viewDash = document.getElementById("view-dashboard");
    this._viewEdit = document.getElementById("view-editor");

    this._bus.on("open-editor",       stem => this._openEditor(stem));
    this._bus.on("back-to-dashboard", ()   => this._showDashboard());
  }

  async start() {
    await this._dashView.show();
  }

  // ── View transitions ──────────────────────────────────────────────────────

  async _openEditor(stem) {
    this._viewDash.classList.remove("active");
    this._viewEdit.classList.add("active");
    await this._editView.open(stem);
  }

  _showDashboard() {
    this._viewEdit.classList.remove("active");
    this._viewDash.classList.add("active");
    // Refresh the list in case a scan completed while in the editor.
    this._dashView.show();
  }
}


// ── Bootstrap ─────────────────────────────────────────────────────────────────

initTheme();

const app = new App();
app.start().catch(console.error);
