"""
services/ocr_service.py — Scan orchestration and SSE streaming.

This is the most complex service: it bridges the blocking, GPU-bound OCR
engine with FastAPI's async event loop using a thread executor and an
asyncio Queue.

Concurrency model
-----------------
Only one scan can run at a time (the model is a GPU singleton).  A
asyncio.Lock enforces this.  Callers that try to start a second scan
while one is running immediately receive an error event and the stream
closes.

SSE wire format
---------------
Each yielded string is a complete SSE message:

    data: {"type":"page","page":3,"total":48,"message":"Page 3 of 48"}\\n\\n

The double newline terminates the SSE message per the spec.
"""

import asyncio
import json
from typing import AsyncGenerator

import ocr_core
from ocr_core import ScanEvent
from services.pdf_service import PdfService


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse(event: ScanEvent) -> str:
    """Serialise a ScanEvent to a single SSE data line."""
    payload = {
        "type":    event.event_type,
        "message": event.message,
        "page":    event.page,
        "total":   event.total,
    }
    return f"data: {json.dumps(payload)}\n\n"


def _sse_error(message: str) -> str:
    """Convenience wrapper for an immediate error event."""
    return _sse(ScanEvent("error", message))


# ── Service ───────────────────────────────────────────────────────────────────

class OcrService:
    """
    Orchestrates full and single-page scans.

    Scan methods return async generators that yield SSE-formatted strings.
    The router mounts these directly onto a StreamingResponse so the browser
    sees live progress without polling.
    """

    def __init__(self, pdf_service: PdfService) -> None:
        self._pdf    = pdf_service
        self._lock   = asyncio.Lock()   # one scan at a time

    # ── Full document scan ────────────────────────────────────────────────────

    async def stream_scan(
        self,
        stem: str,
        page_start: int,
        page_end: int,
    ) -> AsyncGenerator[str, None]:
        """
        Scan a page range and stream SSE progress events back to the client.

        The blocking OCR work runs in a thread-pool executor so the event loop
        remains responsive.  A shared asyncio.Queue bridges the worker thread
        and this async generator.

        Yields SSE strings until a "done" or "error" event terminates the scan.
        """
        if self._lock.locked():
            yield _sse_error("A scan is already in progress")
            return

        async with self._lock:
            if not ocr_core.is_model_loaded():
                yield _sse_error("Model is not loaded — load it from the dashboard first")
                return

            try:
                pdf_path = self._pdf.get_pdf_path(stem)
                out_dir  = self._pdf.get_output_dir(stem)
            except FileNotFoundError as exc:
                yield _sse_error(str(exc))
                return

            queue: asyncio.Queue[ScanEvent] = asyncio.Queue()
            loop = asyncio.get_event_loop()

            def on_progress(event: ScanEvent) -> None:
                # Called from the worker thread — post into the async queue safely.
                asyncio.run_coroutine_threadsafe(queue.put(event), loop)

            def run_scan() -> tuple[list[str], int] | Exception:
                """The blocking scan, executed in a thread pool."""
                try:
                    return ocr_core.process_pdf(
                        pdf_path,
                        out_dir,
                        page_start=page_start,
                        page_end=page_end,
                        on_progress=on_progress,
                    )
                except Exception as exc:
                    # Forward the real error message rather than a generic sentinel.
                    asyncio.run_coroutine_threadsafe(
                        queue.put(ScanEvent("error", str(exc))), loop
                    )
                    return exc

            # Start the blocking work in the thread pool.
            future = loop.run_in_executor(None, run_scan)

            # Relay events from the queue until the scan signals completion.
            while True:
                event = await queue.get()
                yield _sse(event)
                if event.event_type in ("done", "error"):
                    break

            # Await the executor future to propagate any unhandled exceptions.
            result = await future
            if isinstance(result, Exception):
                return

            # Persist the final markdown now that all pages are complete.
            sections, _ = result
            self._pdf.save_markdown(stem, "\n\n---\n\n".join(sections))

    # ── Single-page rescan ────────────────────────────────────────────────────

    async def rescan_page(self, stem: str, page_num: int) -> AsyncGenerator[str, None]:
        """
        Re-OCR a single page and stream progress, then update the markdown.

        Follows the same SSE streaming pattern as stream_scan so the frontend
        can show a spinner with live feedback even for single-page operations.
        """
        if self._lock.locked():
            yield _sse_error("A scan is already in progress")
            return

        async with self._lock:
            try:
                pdf_path = self._pdf.get_pdf_path(stem)
                out_dir  = self._pdf.get_output_dir(stem)
            except FileNotFoundError as exc:
                yield _sse_error(str(exc))
                return

            if not ocr_core.is_model_loaded():
                yield _sse_error("Model is not loaded — load it from the dashboard first")
                return

            queue: asyncio.Queue[ScanEvent | None] = asyncio.Queue()
            loop = asyncio.get_event_loop()

            def on_progress(event: ScanEvent) -> None:
                asyncio.run_coroutine_threadsafe(queue.put(event), loop)

            def run_rescan() -> tuple[list[str], int] | Exception:
                try:
                    return ocr_core.process_pdf(
                        pdf_path,
                        out_dir,
                        page_start=page_num,
                        page_end=page_num,
                        on_progress=on_progress,
                    )
                except Exception as exc:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop)
                    return exc

            future = loop.run_in_executor(None, run_rescan)

            while True:
                event = await queue.get()
                if event is None:
                    yield _sse_error("Rescan thread raised an unexpected exception")
                    break
                yield _sse(event)
                if event.event_type in ("done", "error"):
                    break

            result = await future
            if isinstance(result, Exception):
                return

            # Replace just this page's section in the existing markdown.
            sections, _ = result
            if sections:
                # sections[0] is "<!-- page N -->\n\n{text}" — extract just the text.
                new_text = sections[0].split("\n\n", 2)[-1]
                try:
                    self._pdf.update_page_in_markdown(stem, page_num, new_text)
                except FileNotFoundError:
                    # Edge case: no full markdown yet — save what we have.
                    self._pdf.save_markdown(stem, sections[0])


# ── Dependency factory ────────────────────────────────────────────────────────

_ocr_service: OcrService | None = None

def get_ocr_service() -> OcrService:
    """
    FastAPI Depends() factory.

    Constructs the singleton lazily and injects a PdfService so OcrService
    never reaches into the filesystem directly (Dependency Inversion).
    """
    global _ocr_service
    if _ocr_service is None:
        from services.pdf_service import get_pdf_service
        _ocr_service = OcrService(pdf_service=get_pdf_service())
    return _ocr_service
