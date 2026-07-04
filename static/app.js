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

    this._btnLoad.addEventListener("click",   () => this._loadModel());
    this._btnUnload.addEventListener("click", () => this._unloadModel());
    this._btnScan.addEventListener("click",   () => this._scanSelected());
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
   */
  constructor(container, indicator) {
    this._container  = container;
    this._indicator  = indicator;
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
  }

  async _renderPage(pageNum, wrapper) {
    this._rendered.add(pageNum);

    const page     = await this._pdf.getPage(pageNum);
    // Scale to fill the pane width at ~1.5× viewport ratio for readability.
    const viewport = page.getViewport({ scale: 1.5 });

    const canvas  = document.createElement("canvas");
    canvas.width  = viewport.width;
    canvas.height = viewport.height;

    wrapper.style.minHeight = "";
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
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  getScrollPercent() {
    const { scrollTop, scrollHeight, clientHeight } = this._container;
    const range = scrollHeight - clientHeight;
    return range > 0 ? scrollTop / range : 0;
  }

  setScrollPercent(pct) {
    const { scrollHeight, clientHeight } = this._container;
    this._container.scrollTop = pct * (scrollHeight - clientHeight);
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
      const mid  = el.offsetTop + el.offsetHeight / 2;
      const dist = Math.abs(mid - center);
      if (dist < minDist) {
        minDist = dist;
        closest = parseInt(el.dataset.page, 10);
      }
    }
    return closest;
  }

  /** Return the 1-indexed page number currently most visible (public alias). */
  get currentPage() {
    return this._visiblePageNumber() ?? 1;
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
          theme:         "vs-dark",
          wordWrap:      "on",
          minimap:       { enabled: false },
          fontSize:      14,
          lineNumbers:   "on",
          scrollBeyondLastLine: false,
          // automaticLayout lets Monaco re-measure after Split.js resizes the pane.
          automaticLayout: true,
          padding:       { top: 12 },
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

  /**
   * Listen for content changes.  Returns a disposable (call .dispose() to stop).
   */
  onChange(fn) {
    return this._editor?.onDidChangeModelContent(fn);
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  getScrollPercent() {
    if (!this._editor) return 0;
    const top   = this._editor.getScrollTop();
    const total = this._editor.getScrollHeight() - this._editor.getLayoutInfo().height;
    return total > 0 ? top / total : 0;
  }

  setScrollPercent(pct) {
    if (!this._editor) return;
    const total = this._editor.getScrollHeight() - this._editor.getLayoutInfo().height;
    this._editor.setScrollTop(pct * total);
  }

  onScroll(fn) {
    return this._editor?.onDidScrollChange(fn);
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
    this._debounceTimer = null;
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  /**
   * Schedule a re-render after 150 ms of inactivity.
   * Debouncing prevents the DOM from being replaced on every keypress.
   */
  update(markdownText) {
    clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => this._render(markdownText), 150);
  }

  _render(markdownText) {
    // marked.js is loaded globally from the CDN script tag in index.html.
    this._body.innerHTML = marked.parse(markdownText, { breaks: false });
  }

  // ── Scroll interface (used by SyncScroller) ───────────────────────────────

  getScrollPercent() {
    const { scrollTop, scrollHeight, clientHeight } = this._body;
    const range = scrollHeight - clientHeight;
    return range > 0 ? scrollTop / range : 0;
  }

  setScrollPercent(pct) {
    const { scrollHeight, clientHeight } = this._body;
    this._body.scrollTop = pct * (scrollHeight - clientHeight);
  }

  onScroll(fn) {
    this._body.addEventListener("scroll", fn, { passive: true });
  }
}


// ============================================================================
// SECTION 7: SYNC SCROLL CONTROLLER
// Keeps the editor, preview, and PDF viewer at the same relative scroll
// position (0–100 %).
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
    // Monaco fires a rich scroll-change object; we only need the percentage.
    this._editor.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("editor", this._editor.getScrollPercent());
    });

    this._preview.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("preview", this._preview.getScrollPercent());
    });

    this._pdf.onScroll(() => {
      if (!this._enabled || this._locked) return;
      this._sync("pdf", this._pdf.getScrollPercent());
    });
  }

  /**
   * Propagate a scroll percentage from the source pane to the other two.
   * requestAnimationFrame defers the lock release until after the browser
   * has processed the programmatic scroll events we're about to trigger.
   */
  _sync(source, pct) {
    this._locked = true;

    if (source !== "editor")  this._editor.setScrollPercent(pct);
    if (source !== "preview") this._preview.setScrollPercent(pct);
    if (source !== "pdf")     this._pdf.setScrollPercent(pct);

    requestAnimationFrame(() => { this._locked = false; });
  }
}


// ============================================================================
// SECTION 8: EDITOR VIEW
// Orchestrates the three panes, the Split.js dividers, the toolbar buttons,
// and the auto-save / rescan flows.
// ============================================================================

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
    this._syncToggle   = document.getElementById("toggle-sync-scroll");
    this._overlay      = document.getElementById("rescan-overlay");
    this._overlayMsg   = document.getElementById("rescan-message");

    // Pane elements
    this._monacoContainer = document.getElementById("monaco-container");
    this._previewBody     = document.getElementById("preview-body");
    this._pdfBody         = document.getElementById("pdf-body");
    this._pdfIndicator    = document.getElementById("pdf-page-indicator");

    // Component instances — created once, reused across documents
    this._editorPane  = new MarkdownEditorPane(this._monacoContainer);
    this._previewPane = new PreviewPane(this._previewBody);
    this._pdfPane     = new PdfViewerPane(this._pdfBody, this._pdfIndicator);
    this._scroller    = null;   // created after Monaco is ready

    this._dirty        = false;
    this._monacoReady  = false;
    this._splitInstance = null;

    this._wireToolbar();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  /**
   * Open a document in the editor.
   * Handles first-open Monaco initialisation and subsequent document swaps.
   */
  async open(stem) {
    this._stem  = stem;
    this._dirty = false;
    this._setSaveStatus("");

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

    // Render initial preview
    this._previewPane.update(markdown);
  }

  // ── Split.js — resizable pane dividers ────────────────────────────────────

  _initSplit() {
    if (this._splitInstance) return;   // only create once

    this._splitInstance = Split(
      ["#pane-editor", "#pane-preview", "#pane-pdf"],
      {
        sizes:     [34, 33, 33],
        minSize:   200,
        gutterSize: 6,
        direction: "horizontal",
        // Monaco reflows automatically via automaticLayout:true, but we need
        // to coerce the PDF container width after a drag ends.
        onDragEnd: () => window.dispatchEvent(new Event("resize")),
      },
    );
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
      this._dirty = true;
      this._setSaveStatus("Unsaved", "dirty");
      this._previewPane.update(this._editorPane.getValue());
    });
  }

  // ── Toolbar ───────────────────────────────────────────────────────────────

  _wireToolbar() {
    this._btnBack.addEventListener("click", () => {
      this._bus.emit("back-to-dashboard");
    });

    this._btnSave.addEventListener("click", () => this._save());

    this._btnRescan.addEventListener("click", () => this._rescanCurrentPage());

    // Ctrl+S / Cmd+S to save
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        this._save();
      }
    });
  }

  async _save() {
    if (!this._stem) return;
    this._setSaveStatus("Saving…");
    try {
      await Api.saveMarkdown(this._stem, this._editorPane.getValue());
      this._dirty = false;
      this._setSaveStatus("Saved", "saved");
      setTimeout(() => this._setSaveStatus(""), 2000);
    } catch {
      this._setSaveStatus("Save failed", "dirty");
    }
  }

  _setSaveStatus(text, modifier = "") {
    this._saveStatusEl.textContent  = text;
    this._saveStatusEl.className    = `save-status ${modifier}`;
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
      this._dirty = false;
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

const app = new App();
app.start().catch(console.error);
