"""
routers/scanner.py — Full document scan and single-page rescan endpoints.

Both endpoints return Server-Sent Event streams so the frontend receives
live progress without polling.  EventSource (GET-only in browsers) drives
the full scan; a fetch()-based SSE reader handles the rescan.

Why GET for scan?
  The standard EventSource API only supports GET requests.  Page range
  parameters fit naturally as query params, so there's no need for a body.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from services.ocr_service import OcrService, get_ocr_service

router = APIRouter()

# SSE headers: disable buffering so events reach the browser immediately.
_SSE_HEADERS = {
    "Cache-Control":    "no-cache",
    "X-Accel-Buffering": "no",   # prevents nginx from buffering the stream
}


@router.get("/pdf/{stem}/scan")
async def scan_pdf(
    stem: str,
    page_start: int = Query(default=1, ge=1),
    page_end:   int = Query(default=0, ge=0),   # 0 means "scan to last page"
    svc: OcrService = Depends(get_ocr_service),
):
    """
    Start a full (or range) scan and stream SSE progress events.

    page_end=0 is treated as "scan to the last page of the document" by
    ocr_core.process_pdf, which clamps page_end to len(doc).

    The stream closes automatically when the engine emits a "done" or
    "error" event.
    """
    effective_end = page_end if page_end > 0 else None

    return StreamingResponse(
        svc.stream_scan(stem, page_start=page_start, page_end=effective_end),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/pdf/{stem}/rescan/{page}")
async def rescan_page(
    stem: str,
    page: int,
    svc: OcrService = Depends(get_ocr_service),
):
    """
    Re-OCR a single page and stream SSE progress, then patch the markdown.

    The frontend uses fetch() with a ReadableStream reader rather than
    EventSource because it needs to trigger this from user interaction
    (clicking the Rescan button) rather than a passive subscription.
    """
    return StreamingResponse(
        svc.rescan_page(stem, page),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
