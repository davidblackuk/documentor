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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import ocr_core
from routers import model, pdfs, scanner

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


# "localhost" and "127.0.0.1" are different origins to the browser, so
# per-document localStorage (font sizes, edit positions) silently forks
# depending on which hostname was used. Canonicalize on 127.0.0.1, which
# is what start.sh binds and prints, so localStorage stays under one origin.
@app.middleware("http")
async def canonicalize_host(request: Request, call_next):
    host = request.headers.get("host", "")
    if host.startswith("localhost:") or host == "localhost":
        new_host = host.replace("localhost", "127.0.0.1", 1)
        url = request.url.replace(netloc=new_host)
        return RedirectResponse(url=str(url), status_code=307)
    return await call_next(request)

# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(pdfs.router,    prefix="/api")
app.include_router(scanner.router, prefix="/api")
app.include_router(model.router,   prefix="/api")

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
