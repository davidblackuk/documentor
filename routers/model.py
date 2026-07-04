"""
routers/model.py — Model load/unload endpoints.

Model loading takes ~30 seconds and emits progress messages, so the load
endpoint streams SSE just like the scanner.  Unload is instant and returns
plain JSON.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

import ocr_core
from ocr_core import ScanEvent
from services.model_service import ModelService, get_model_service

import asyncio
import json

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control":     "no-cache",
    "X-Accel-Buffering": "no",
}

def _sse(event: ScanEvent) -> str:
    payload = {"type": event.event_type, "message": event.message}
    return f"data: {json.dumps(payload)}\n\n"


@router.get("/model")
def get_model_status(svc: ModelService = Depends(get_model_service)):
    """Return current model state (loaded flag and model name)."""
    return asdict(svc.get_status())


@router.get("/model/load")
async def load_model(svc: ModelService = Depends(get_model_service)):
    """
    Load the model and stream SSE progress events during initialisation.

    The browser subscribes with EventSource so it can update the UI header
    in real time without blocking on a long HTTP response.
    """
    queue: asyncio.Queue[ScanEvent | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_progress(event: ScanEvent) -> None:
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def run_load() -> None:
        try:
            svc.load(on_progress=on_progress)
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(
                queue.put(ScanEvent("error", str(exc))), loop
            )
        finally:
            # Sentinel to close the generator.
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    async def event_stream():
        loop.run_in_executor(None, run_load)
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)
            if event.event_type in ("error",):
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/model/unload")
def unload_model(svc: ModelService = Depends(get_model_service)):
    """Unload the model from VRAM synchronously and return immediately."""
    svc.unload()
    return {"status": "ok"}
