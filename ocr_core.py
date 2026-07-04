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

import io, os, re, shutil, tempfile, warnings
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Silence transformer/model loading noise before any HF import.
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
warnings.filterwarnings("ignore")

import fitz          # PyMuPDF — PDF rendering
import torch

# ── Directory layout ──────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
PREVIEW_DIR = BASE_DIR / "preview"

# Hardcoded weights path — update DEEPSEEK_PATH here if you move the model.
DEEPSEEK_PATH = Path(
    "/home/davidb/Documents/AI/ComfyUI/models/deepseek-ocr/deepseek-ai_DeepSeek-OCR"
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
    "start" — scan is starting; total is set
    "page"  — one page finished; page and total are set
    "done"  — scan complete
    "error" — a page failed; page is set, message has the reason
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
_OCR_PROMPT = "<image>\n<|grounding|>Convert the document to markdown. "


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

def _ocr_page(
    img_path: str,
    page_tmp: Path,
    page_num: int,
    imgs_dir: Path,
    img_counter: int,
) -> tuple[str, int]:
    """
    Run DeepSeek-OCR on one rendered PNG and return (markdown_text, img_counter).

    Extracted figures are renamed to p{page:04d}_{idx:04d}{ext} and copied
    into imgs_dir so every output document shares one flat images/ folder.
    The raw mmd text has figure references rewritten to match those new names.

    img_counter is passed in and returned incremented so the caller can
    maintain a monotonically increasing global index across pages.
    """
    page_tmp.mkdir(exist_ok=True)

    # Model output is noisy; suppress it so only our progress events surface.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        _model.infer(
            _tokenizer,
            prompt=_OCR_PROMPT,
            image_file=img_path,
            output_path=str(page_tmp),
            base_size=1024,
            image_size=640,
            crop_mode=True,     # tile large pages for better quality
            save_results=True,
            test_compress=True,
        )

    torch.cuda.empty_cache()

    mmd  = page_tmp / "result.mmd"
    text = mmd.read_text(encoding="utf-8") if mmd.exists() else "[NO OUTPUT]"

    # Rename and relocate any figures the model extracted from this page.
    src_imgs = page_tmp / "images"
    if src_imgs.is_dir():
        for fname in sorted(os.listdir(src_imgs)):
            ext  = Path(fname).suffix
            dest = f"p{page_num:04d}_{img_counter:04d}{ext}"
            shutil.copy2(src_imgs / fname, imgs_dir / dest)
            text = text.replace(f"images/{fname}", f"images/{dest}")
            img_counter += 1

    return strip_tags(text), img_counter


# ── Core OCR — full document ──────────────────────────────────────────────────

def process_pdf(
    pdf_path: Path,
    out_dir: Path,
    page_start: int = 1,
    page_end: int | None = None,
    on_progress: ProgressCallback = _noop,
) -> tuple[list[str], int]:
    """
    Render a PDF page-range to PNGs and OCR each page sequentially.

    Returns (sections, img_count) where sections is a list of strings, one
    per page, each prefixed with an HTML comment <!-- page N --> so the web
    editor can locate page boundaries.  The caller joins them with a page
    separator and writes the final .md file.

    A partial file (<stem>.partial.md) is written after every page so the
    scan can be inspected mid-run and resumed manually after a crash.
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
        for page_num, img_path in page_imgs:
            page_tmp = tmp / f"ocr_{page_num:04d}"
            try:
                text, img_counter = _ocr_page(
                    img_path, page_tmp, page_num, imgs_dir, img_counter
                )
            except Exception as exc:
                torch.cuda.empty_cache()
                text = f"[OCR ERROR page {page_num}: {exc}]"
                on_progress(ScanEvent("error", str(exc), page=page_num, total=len(pages)))

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
