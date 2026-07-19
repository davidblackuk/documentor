#!/usr/bin/env python3
"""
server.py — FastAPI application entry point.

Responsibilities (and nothing else):
  • Create the FastAPI app instance
  • Register all API routers under /api
  • Mount the static frontend at /
  • Ensure input/output/preview directories exist on startup

Everything else — model management, file I/O, OCR logic — lives in the
services and routers packages.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import ocr_core
from routers import backup, blog, formatting, model, pdfs, preferences, scanner

# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="DocuMentor",
    description="PDF → Markdown converter with browser-based editor",
    version="2.0.0",
)

# Allow the Vite dev server (port 5173) to call the API during development.
# In production the frontend is served from the same origin so this is a no-op.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(pdfs.router,        prefix="/api")
app.include_router(scanner.router,     prefix="/api")
app.include_router(model.router,       prefix="/api")
app.include_router(formatting.router,  prefix="/api")
app.include_router(backup.router,      prefix="/api")
app.include_router(blog.router,        prefix="/api")
app.include_router(preferences.router, prefix="/api")

# ── Static frontend ───────────────────────────────────────────────────────────

# Mounted last so /api routes take priority over the catch-all static handler.
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def create_directories() -> None:
    """Create the working directories if they don't exist yet."""
    for d in (ocr_core.INPUT_DIR, ocr_core.OUTPUT_DIR, ocr_core.PREVIEW_DIR):
        d.mkdir(exist_ok=True)
