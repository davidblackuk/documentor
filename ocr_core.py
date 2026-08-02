#!/usr/bin/env python3
"""
ocr_core.py — Pure OCR engine, no UI or framework dependencies.

Extracted from documenter.py so the web server and the legacy CLI
can share model management and PDF processing without coupling UI
decisions to domain logic (Dependency Inversion, Single Responsibility).

Public surface
--------------
  ScanEvent          — progress event emitted during load/scan
  ProgressCallback   — type alias for the callback signature
  load_model()       — load DeepSeek-OCR into GPU VRAM (idempotent)
  unload_model()     — free VRAM and reset singleton state
  is_model_loaded()  — quick status check
  process_pdf()      — render pages → OCR → list of markdown sections
  slugify()          — shared text utility
"""

import io, os, re, shutil, tempfile, threading, warnings
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Silence transformer/model loading noise before any HF import.
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
warnings.filterwarnings("ignore")

import fitz          # PyMuPDF — PDF rendering
import numpy as np
import torch
from PIL import Image
from scipy import ndimage

# ── Directory layout ──────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
PREVIEW_DIR = BASE_DIR / "preview"
PREFS_PATH  = BASE_DIR / "preferences.json"

# Hardcoded weights path — update DEEPSEEK_PATH here if you move the model.
DEEPSEEK_PATH = Path(
    "/home/davidb/Documents/AI/ComfyUI/models/deepseek-ocr2/deepseek-ai_DeepSeek-OCR-2"
)

# ── Progress event contract ───────────────────────────────────────────────────

@dataclass
class ScanEvent:
    """
    A single progress notification emitted during model loading or PDF scanning.

    Consumers (CLI, SSE stream, test harness) receive these via a callback and
    can format them however they like without touching the engine code.

    event_type values
    -----------------
    "log"   — informational message (no page context)
    "start"     — scan is starting; total is set
    "page"      — one page finished; page and total are set
    "done"      — scan complete
    "error"     — a page failed; page is set, message has the reason
    "cancelled" — scan was stopped early by request; page/total reflect progress so far
    """
    event_type: str
    message: str
    page: int | None = None
    total: int | None = None


# Callable that accepts a ScanEvent and returns nothing.
ProgressCallback = Callable[[ScanEvent], None]

def _noop(_: ScanEvent) -> None:
    """Default no-op callback used when the caller passes nothing."""
    pass


# ── Model singleton ───────────────────────────────────────────────────────────
# The model lives in VRAM for the process lifetime once loaded.
# Both load_model and unload_model mutate these globals.

_model     = None
_tokenizer = None

# Prompt expected by DeepSeek-OCR's infer() method.
#
# The grounding/markdown-conversion prompt ("<|grounding|>Convert the
# document to markdown.") trains the model to treat "$...$" as LaTeX math
# delimiters, which silently corrupts hex literals in 68000 assembly
# listings (e.g. "$FFFF" -> "\mathbb{S}...7777") and the ".l"/".w"/".b" size
# suffixes (".l" -> ".1"). Plain "Free OCR." mode avoids both while still
# preserving multi-row table/parameter-list structure correctly.
_OCR_PROMPT = "<image>\nFree OCR. "

# Fallback prompt, used only when the primary pass looks like a runaway
# repetition loop (see _looks_like_repetition_loop). Sparse/mostly-blank
# pages (e.g. chapter dividers) can send "Free OCR" mode into hundreds of
# repeats of an unrelated sentence instead of recognizing the page as
# blank. The grounding prompt doesn't have that failure mode and is also
# the only one of the two that extracts embedded figures into images/, at
# the cost of the "$...$" and ".l" corruption described above — an
# acceptable trade on the rare pages that need it.
_OCR_PROMPT_GROUNDING = "<image>\n<|grounding|>Convert the document to markdown. "

# Below this many non-blank lines there's not enough signal either way to
# call a page a repetition loop, so it's always left alone.
_REPEAT_MIN_LINES = 4

# A line-count-ratio version of this check (what fraction of *lines* are
# the most common normalized line) over-triggers on this book's single
# most common legitimate pattern: a short indexed parameter list like
# "vdpb(0)" .. "vdpb(4)", which digit-stripping collapses into 5 identical
# "vdpb" lines. On a short page that's enough lines to look like a loop
# even though the rest of the page is perfectly good prose. Weighting by
# *characters* instead fixes that (a handful of 4-char labels can't
# dominate a page that also has full sentences) while still catching
# assembly listings that legitimately repeat short opcodes ("rts", "trap
# #1") verbatim many times - those max out around 0.6, real loops run
# 0.9-1.0, so the threshold sits in the gap between them.
_REPEAT_CHAR_MASS_RATIO = 0.75


def _looks_like_repetition_loop(text: str) -> bool:
    """
    Detect the runaway-generation failure mode described above: the model
    gets stuck emitting near-duplicate lines over and over instead of
    recognizing a sparse page as sparse. Two variants seen in practice:
    the same line repeated with only an incrementing counter changing, and
    a short caption-like phrase repeated with slightly different wording
    each time (e.g. "[Image of a logo or emblem]" / "[Image of an emblem
    or logo]" alternating). Both are caught by normalizing away digits and
    punctuation before comparing lines, rather than requiring byte-exact
    repeats - then weighting each duplicate group by how many characters
    of the total page it accounts for, not just how many lines, so a
    short repeated label can't outweigh substantial surrounding prose.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < _REPEAT_MIN_LINES:
        return False

    # Per normalized form: (occurrence count, total raw characters across
    # all its occurrences).
    groups: dict[str, list[int]] = {}
    total_chars = 0
    for ln in lines:
        norm = re.sub(r"[^\w]", "", re.sub(r"\d+", "", ln), flags=re.UNICODE).lower()
        total_chars += len(ln)
        if norm:
            groups.setdefault(norm, []).append(len(ln))

    if total_chars == 0:
        return False

    # Only lines whose normalized form recurs (an actual duplicate, not
    # just one long one-off sentence) count toward the repeated mass.
    dup_chars = sum(
        sum(char_lens) for char_lens in groups.values() if len(char_lens) >= 2
    )
    return dup_chars / total_chars >= _REPEAT_CHAR_MASS_RATIO


# Fraction of "dark" pixels below which a rendered page is treated as
# sparse/blank (e.g. a chapter-divider page with just a faint logo).
# Calibrated against this book: real sparse pages measured 0.00000-0.00008,
# real dense pages (prose, tables, code) measured 0.020-0.041 - a 250x+
# margin either side of this threshold, so it's not a close call.
_SPARSE_INK_RATIO = 0.005
_SPARSE_DARK_THRESHOLD = 200  # 0-255 grayscale; below this counts as "ink"


def _page_looks_sparse(img_path: str) -> bool:
    """
    Detect a sparse/near-blank rendered page directly from its pixels,
    before OCR ever runs.

    This exists because text-based repetition detection (see
    _looks_like_repetition_loop) can't catch every way "Free OCR" mode
    hallucinates on a blank page - it can wander into an extended,
    non-repetitive fabrication (a fake multi-paragraph FAQ document was
    observed on one such page) or produce one long run-on line with no
    line boundaries for the repetition check to compare (a page "reading"
    an imagined unrelated image and counting upward was observed on
    another). Neither has a text-level repetition signature to catch.
    Checking the source image directly sidesteps the problem: a blank
    page is detectably blank regardless of what the model would go on to
    hallucinate about it.
    """
    img = Image.open(img_path).convert("L")
    arr = np.asarray(img)
    ink_ratio = (arr < _SPARSE_DARK_THRESHOLD).sum() / arr.size
    return ink_ratio < _SPARSE_INK_RATIO


# Below this fractional size (of body width/height, whichever is smaller) a
# connected ink component isn't treated as a diagram. Calibrated against this
# book series: real diagrams (register maps, disk-sector illustrations,
# schematic boxes) score 0.27-0.46 on this measure; prose and even dense
# tables (whose rule lines are long in one dimension only) top out around
# 0.02-0.04 - a wide margin either side of this threshold.
_DIAGRAM_MIN_COMPONENT_FRACTION = 0.15

# Exclude running headers/footers (title bar + rule line, page number) from
# the diagram scan. A header rule line is itself one long, thin connected
# component that would otherwise read as "large" on every page regardless
# of content, since it's long in the width dimension on every single page.
_DIAGRAM_TOP_MARGIN = 0.09
_DIAGRAM_BOTTOM_MARGIN = 0.07

# Connected-component analysis is done on a downscaled copy - full 300 DPI
# resolution isn't needed to tell "large 2-D graphic" from "text", and
# downscaling keeps the labeling pass fast on ~2800x2000px page renders.
_DIAGRAM_DOWNSCALE = 4


def _page_has_diagram(img_path: str) -> bool:
    """
    Detect whether a rendered page likely contains embedded line-art (a
    diagram, schematic, or illustration) by looking at the rendered pixels
    directly, rather than the source PDF's image metadata.

    These source PDFs are scan-per-page: every page - text or diagram alike
    - is one full-page raster with no separately embeddable figure object,
    so checking the PDF's own image list (fitz Page.get_images()) can't
    distinguish them; it reports the same "one image covering 100% of the
    page" regardless of content (verified against several of these books).

    Instead: text characters and table rule lines each produce connected
    ink components that are large in at most one dimension (a rule line is
    wide but only a few pixels tall; a character is small in both). A
    diagram's outline - a circle, a register map, a schematic box - produces
    at least one component that's large in *both* width and height
    simultaneously. Taking min(width_fraction, height_fraction) per
    component, and the max of that across all components, cleanly separates
    the two cases (see _DIAGRAM_MIN_COMPONENT_FRACTION for the calibration
    numbers behind the threshold).

    This exists because figure extraction only happens as a side effect of
    routing a page through the grounding prompt (see _ocr_page); a more
    reliable model that rarely needs that fallback would otherwise silently
    stop extracting figures on pages it OCRs correctly on the first try.
    """
    img = Image.open(img_path).convert("L")
    arr = np.asarray(img)
    h, w = arr.shape
    body = arr[int(h * _DIAGRAM_TOP_MARGIN):int(h * (1 - _DIAGRAM_BOTTOM_MARGIN)), :]
    small = body[::_DIAGRAM_DOWNSCALE, ::_DIAGRAM_DOWNSCALE]
    dark = small < _SPARSE_DARK_THRESHOLD

    labels, n = ndimage.label(dark, structure=np.ones((3, 3)))
    if n == 0:
        return False

    bh, bw = small.shape
    for sl in ndimage.find_objects(labels):
        comp_h = sl[0].stop - sl[0].start
        comp_w = sl[1].stop - sl[1].start
        if min(comp_w / bw, comp_h / bh) >= _DIAGRAM_MIN_COMPONENT_FRACTION:
            return True
    return False


# ── Public helpers ────────────────────────────────────────────────────────────

def is_model_loaded() -> bool:
    """Return True if the model is resident in VRAM."""
    return _model is not None


def strip_tags(text: str) -> str:
    """
    Remove DeepSeek layout annotation tags from raw OCR output.

    The model sometimes wraps detected regions in <|ref|>…<|/ref|> and
    <|det|>…<|/det|> tags that are meaningful internally but noisy in the
    final markdown.
    """
    text = re.sub(r"<\|ref\|>.*?<\|/ref\|>", "", text)
    text = re.sub(r"<\|det\|>.*?<\|/det\|>", "", text)
    return text.strip()


def slugify(name: str) -> str:
    """Convert a display name to a lowercase, hyphen-separated slug."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


# ── Model management ──────────────────────────────────────────────────────────

def load_model(on_progress: ProgressCallback = _noop) -> None:
    """
    Load DeepSeek-OCR into GPU VRAM (idempotent — safe to call if already loaded).

    The transformers model emits a lot of console noise during loading; we
    redirect stdout/stderr to suppress it while still forwarding human-readable
    status through the progress callback.
    """
    global _model, _tokenizer

    if _model is not None:
        on_progress(ScanEvent("log", "Model already loaded"))
        return

    on_progress(ScanEvent("log", "Loading DeepSeek-OCR into VRAM…"))

    from transformers import AutoModel, AutoTokenizer

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        _tokenizer = AutoTokenizer.from_pretrained(
            str(DEEPSEEK_PATH), trust_remote_code=True
        )
        _model = AutoModel.from_pretrained(
            str(DEEPSEEK_PATH),
            _attn_implementation="eager",   # avoid flash-attention issues
            trust_remote_code=True,
            use_safetensors=True,
        )
        _model = _model.eval().cuda().to(torch.bfloat16)

    on_progress(ScanEvent("log", "Model ready"))


def unload_model() -> None:
    """
    Release the model from VRAM and clear the CUDA cache.

    After this call, is_model_loaded() returns False and process_pdf()
    will raise RuntimeError until load_model() is called again.
    """
    global _model, _tokenizer

    if _model is None:
        return

    del _model, _tokenizer
    _model = _tokenizer = None
    torch.cuda.empty_cache()


# ── Core OCR — single page ────────────────────────────────────────────────────

def _run_infer(prompt: str, img_path: str, out_dir: Path) -> str:
    """Run one DeepSeek-OCR inference pass and return the raw result.mmd text."""
    # Model output is noisy; suppress it so only our progress events surface.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        _model.infer(
            _tokenizer,
            prompt=prompt,
            image_file=img_path,
            output_path=str(out_dir),
            base_size=1024,
            image_size=768,     # DeepSeek-OCR-2's tuned tile resolution
            crop_mode=True,     # tile large pages for better quality
            save_results=True,
            test_compress=True,
        )

    torch.cuda.empty_cache()

    mmd = out_dir / "result.mmd"
    return mmd.read_text(encoding="utf-8") if mmd.exists() else "[NO OUTPUT]"


def _ocr_page(
    img_path: str,
    page_tmp: Path,
    page_num: int,
    imgs_dir: Path,
    img_counter: int,
) -> tuple[str, int, bool, bool]:
    """
    Run DeepSeek-OCR on one rendered PNG and return
    (markdown_text, img_counter, used_fallback, used_figure_pass).

    Checks the rendered page for sparseness first (see _page_looks_sparse)
    and routes straight to the grounding prompt for pages that look
    blank - Free OCR mode is unreliable there in ways that don't always
    show up as detectable repetition (see that function's docstring).
    Otherwise tries the primary Free OCR prompt, falling back to
    grounding mode if the output looks like a runaway repetition loop
    anyway. used_fallback tells the caller whether grounding mode ended
    up being used either way, so it can be surfaced as a progress log line.

    Figure extraction only happens as a side effect of a grounding-mode
    pass, so a page that used neither fallback path above but still looks
    like it contains a diagram (see _page_has_diagram) gets one extra
    grounding-mode pass whose sole purpose is harvesting figures - the
    primary Free OCR text from above is kept as-is rather than replaced,
    since grounding mode's markdown conversion is what corrupts hex
    literals and size suffixes in the first place (see _OCR_PROMPT_GROUNDING).
    used_figure_pass tells the caller whether this happened, so it can be
    surfaced as a progress log line distinct from used_fallback.

    Extracted figures are renamed to p{page:04d}_{idx:04d}{ext} and copied
    into imgs_dir so every output document shares one flat images/ folder.
    Where the raw mmd text already contains a matching figure reference
    (grounding mode's own output does) it's rewritten to the new name;
    otherwise (the supplementary figure-pass case, where the kept text
    never mentioned the image at all) a markdown image link is appended.

    img_counter is passed in and returned incremented so the caller can
    maintain a monotonically increasing global index across pages.
    """
    page_tmp.mkdir(exist_ok=True)

    if _page_looks_sparse(img_path):
        result_dir = page_tmp / "fallback"
        text = _run_infer(_OCR_PROMPT_GROUNDING, img_path, result_dir)
        used_fallback = True
    else:
        result_dir = page_tmp / "primary"
        text = _run_infer(_OCR_PROMPT, img_path, result_dir)
        used_fallback = False

        if _looks_like_repetition_loop(text):
            result_dir = page_tmp / "fallback"
            text = _run_infer(_OCR_PROMPT_GROUNDING, img_path, result_dir)
            used_fallback = True

    used_figure_pass = False
    if not used_fallback and _page_has_diagram(img_path):
        figure_dir = page_tmp / "figures"
        _run_infer(_OCR_PROMPT_GROUNDING, img_path, figure_dir)
        if (figure_dir / "images").is_dir() and os.listdir(figure_dir / "images"):
            result_dir = figure_dir
            used_figure_pass = True

    # Rename and relocate any figures the model extracted from this page.
    src_imgs = result_dir / "images"
    if src_imgs.is_dir():
        appended = []
        for fname in sorted(os.listdir(src_imgs)):
            ext  = Path(fname).suffix
            dest = f"p{page_num:04d}_{img_counter:04d}{ext}"
            shutil.copy2(src_imgs / fname, imgs_dir / dest)
            if f"images/{fname}" in text:
                text = text.replace(f"images/{fname}", f"images/{dest}")
            else:
                appended.append(dest)
            img_counter += 1
        if appended:
            text += "\n\n" + "\n".join(f"![]({'images/' + d})" for d in appended)

    return strip_tags(text), img_counter, used_fallback, used_figure_pass


# ── Core OCR — full document ──────────────────────────────────────────────────

def process_pdf(
    pdf_path: Path,
    out_dir: Path,
    page_start: int = 1,
    page_end: int | None = None,
    on_progress: ProgressCallback = _noop,
    cancel_event: threading.Event | None = None,
) -> tuple[list[str], int]:
    """
    Render a PDF page-range to PNGs and OCR each page sequentially.

    Returns (sections, img_count) where sections is a list of strings, one
    per page, each prefixed with an HTML comment <!-- page N --> so the web
    editor can locate page boundaries.  The caller joins them with a page
    separator and writes the final .md file.

    A partial file (<stem>.partial.md) is written after every page so the
    scan can be inspected mid-run and resumed manually after a crash.

    If cancel_event is set between pages, the scan stops before starting
    the next page (an in-flight page's OCR always runs to completion —
    interrupting a model.infer() call mid-generation isn't safe), emits a
    "cancelled" event instead of "done", and leaves the partial file in
    place rather than deleting it.
    """
    if _model is None:
        raise RuntimeError("Model not loaded — call load_model() first")

    imgs_dir = out_dir / "images"
    imgs_dir.mkdir(parents=True, exist_ok=True)

    doc      = fitz.open(str(pdf_path))
    total    = len(doc)
    page_end = min(page_end or total, total)
    pages    = range(page_start - 1, page_end)

    on_progress(ScanEvent("start", f"Scanning {pdf_path.name}", total=len(pages)))

    sections    = []
    img_counter = 0
    partial_md  = out_dir / f"{out_dir.name}.partial.md"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── Phase 1: render all pages to PNG (fast, CPU-bound) ────────────────
        page_imgs = []
        for i in pages:
            mat = fitz.Matrix(300 / 72, 300 / 72)   # 300 DPI
            pix = doc[i].get_pixmap(matrix=mat)
            p   = tmp / f"page_{i + 1:04d}.png"
            pix.save(str(p))
            page_imgs.append((i + 1, str(p)))
        doc.close()

        # ── Phase 2: OCR each page (slow, GPU-bound) ──────────────────────────
        cancelled = False
        for page_num, img_path in page_imgs:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break

            page_tmp = tmp / f"ocr_{page_num:04d}"
            used_fallback = False
            used_figure_pass = False
            try:
                text, img_counter, used_fallback, used_figure_pass = _ocr_page(
                    img_path, page_tmp, page_num, imgs_dir, img_counter
                )
            except Exception as exc:
                torch.cuda.empty_cache()
                text = f"[OCR ERROR page {page_num}: {exc}]"
                on_progress(ScanEvent("error", str(exc), page=page_num, total=len(pages)))

            if used_fallback:
                on_progress(ScanEvent(
                    "log",
                    f"Page {page_num}: sparse page or repetition loop detected, used grounding mode",
                    page=page_num,
                    total=len(pages),
                ))
            elif used_figure_pass:
                on_progress(ScanEvent(
                    "log",
                    f"Page {page_num}: diagram detected, ran supplementary figure-extraction pass",
                    page=page_num,
                    total=len(pages),
                ))

            # The <!-- page N --> marker lets the editor find page boundaries.
            sections.append(f"<!-- page {page_num} -->\n\n{text}")

            # Write partial file after each page so crashes don't lose all work.
            partial_md.write_text(
                "\n\n---\n\n".join(sections), encoding="utf-8"
            )

            on_progress(ScanEvent(
                "page",
                f"Page {page_num} of {page_end}",
                page=page_num,
                total=len(pages),
            ))

    if cancelled:
        # Leave the partial file in place — it reflects real progress made
        # before the cancel request landed.
        on_progress(ScanEvent(
            "cancelled",
            f"Cancelled after {len(sections)} of {len(pages)} pages",
            page=len(sections),
            total=len(pages),
        ))
        return sections, img_counter

    # Clean up the partial file now that the full scan succeeded.
    if partial_md.exists():
        partial_md.unlink()

    on_progress(ScanEvent(
        "done",
        f"Complete — {len(sections)} pages, {img_counter} images",
        page=len(sections),
        total=len(sections),
    ))

    return sections, img_counter
