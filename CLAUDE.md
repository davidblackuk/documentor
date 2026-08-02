# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DocuMentor — a terminal UI pipeline that converts PDF files to Markdown using DeepSeek-OCR (a vision-language model). It renders PDF pages to PNG at 300 DPI via PyMuPDF, runs them through the model one page at a time, collects extracted figures, and writes a single `.md` file per PDF.

## Running the tool

```bash
# Activate the venv and launch the TUI (preferred)
./start.sh

# Or manually:
source bin/activate
python documenter.py

# Headless batch mode (legacy, no TUI):
python run_ocr.py [input_dir] [output_dir]
```

The venv was created from the ComfyUI Python 3.12 interpreter. Activate it before running anything.

## Directory layout

```
input/      ← drop PDFs here
output/     ← one subdirectory per PDF: <stem>/<stem>.md + images/
preview/    ← temporary output from Test Scan (page-range previews)
```

## Architecture

**`documenter.py`** is the main entrypoint. Key sections:

- `DEEPSEEK_PATH` — hardcoded path to the model weights (`~/Documents/AI/ComfyUI/models/deepseek-ocr2/deepseek-ai_DeepSeek-OCR-2`). Must exist for DeepSeek to load.
- `load_model()` / `unload_model()` — lazy singleton; model stays in GPU VRAM until explicitly unloaded or the process exits.
- `ocr_page()` — calls `_model.infer(...)` with `crop_mode=True`, reads `result.mmd` from the temp output dir, rewrites embedded image refs to the shared `images/` folder, strips DeepSeek layout tags (`<|ref|>`, `<|det|>`).
- `process_pdf()` — renders all selected pages to PNGs in a `tempfile.TemporaryDirectory`, then OCRs them sequentially with a Rich progress bar.
- Menu actions (`action_scan_new`, `action_rescan`, `action_test_scan`, `action_view_glow`, `action_select_model`) drive the interactive flow.

**`run_ocr.py`** is an older, simpler version of the same pipeline — no TUI, no resume logic, processes all PDFs in `input/` unconditionally (unless the master `.md` already exists). Kept for reference/headless use.

## Model details

- **DeepSeek-OCR-2** loaded via `transformers.AutoModel` with `trust_remote_code=True`, `bfloat16`, CUDA. Uses `_attn_implementation="eager"` to avoid flash-attention issues (the model card's example uses `flash_attention_2`, but the vision encoder rejects that implementation anyway and `eager` works fine without installing `flash-attn`).
- `model.infer(...)` is the model's custom method (not a standard HF API). Parameters `base_size=1024`, `image_size=768`, `crop_mode=True` are load-bearing — changing them affects crop tiling and output quality. `image_size=768` is OCR-2's tuned tile resolution (v1 used 640).
- Output file is `result.mmd` in the temp directory passed as `output_path`.
- `MODELS["marker"]` stub exists for future integration but is not implemented.
- Figure extraction only happens as a side effect of a grounding-mode pass (Free OCR mode doesn't detect image regions). `ocr_core._page_has_diagram()` uses connected-component analysis (`scipy.ndimage.label`) on the rendered page pixels to catch diagram-bearing pages that would otherwise OCR cleanly with Free OCR and never trigger a fallback — see that function's docstring for why PDF-level image metadata (`fitz.Page.get_images()`) doesn't work for this (these source PDFs are scan-per-page, so every page reports one full-page image regardless of content). `scipy` is an added dependency for this.

## Git conventions

- Do not add a `Co-Authored-By: Claude` (or similar attribution) line to commit messages.

## Known issues

- Code pages (dense text, listings) sometimes trigger a repetition loop in the model, producing very long outputs. No mitigation is currently in place.
- `glow` is an optional dependency for in-terminal Markdown preview — install with `sudo apt install glow`.
- `astyle` is required for the web editor's "Code Block" C/C# reformatting (`services/format_service.py`) — install with `sudo apt install astyle`. BASIC and Z80/MC68000 assembler formatting are hand-rolled (no such formatter exists for either) and don't need it.
