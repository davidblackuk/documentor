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
from routers import formatting, model, pdfs, scanner

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
#
# /api/* is excluded: redirecting an in-page fetch() cross-origin requires
# CORS headers on the final response, which we don't send for localhost,
# so the browser blocks it. The API behaves identically on either
# hostname, so there's nothing to gain by redirecting it anyway — only
# the document/static routes need to converge for localStorage's sake.
@app.middleware("http")
async def canonicalize_host(request: Request, call_next):
    host = request.headers.get("host", "")
    is_localhost = host.startswith("localhost:") or host == "localhost"
    is_api = request.url.path.startswith("/api/")
    if is_localhost and not is_api:
        new_host = host.replace("localhost", "127.0.0.1", 1)
        url = request.url.replace(netloc=new_host)
        return RedirectResponse(url=str(url), status_code=307)
    response = await call_next(request)
    if not is_api:
        # StaticFiles sends only Last-Modified/ETag, so browsers apply
        # heuristic caching and can serve a stale localhost:8000 document
        # straight from disk cache without ever hitting the server again
        # — which would silently skip the redirect above. Force
        # revalidation on every navigation so that can't happen.
        response.headers["Cache-Control"] = "no-cache"
    return response

# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(pdfs.router,      prefix="/api")
app.include_router(scanner.router,   prefix="/api")
app.include_router(model.router,     prefix="/api")
app.include_router(formatting.router, prefix="/api")

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
