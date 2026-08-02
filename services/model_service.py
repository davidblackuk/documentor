"""
services/model_service.py — Model lifecycle management.

Single responsibility: knows whether the model is loaded, can load it,
and can unload it.  Delegates all actual model I/O to ocr_core so this
service stays focused on state and coordination.
"""

from dataclasses import dataclass

import ocr_core
from ocr_core import ProgressCallback, _noop


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ModelStatus:
    """Current state of the OCR model."""
    loaded: bool
    name: str          # human-readable model name for the UI header


# ── Service ───────────────────────────────────────────────────────────────────

class ModelService:
    """
    Thin coordinator for model load/unload operations.

    Load is intentionally synchronous in the calling thread because the
    web server runs it inside run_in_executor, keeping the async event
    loop free while transformers does its multi-second initialisation.
    """

    _MODEL_NAME = "DeepSeek-OCR-2"

    def get_status(self) -> ModelStatus:
        """Return the current model state without any side effects."""
        return ModelStatus(
            loaded=ocr_core.is_model_loaded(),
            name=self._MODEL_NAME,
        )

    def load(self, on_progress: ProgressCallback = _noop) -> None:
        """
        Load the model into VRAM.

        Idempotent — if the model is already loaded, on_progress receives
        a single "log" event and the call returns immediately.
        """
        ocr_core.load_model(on_progress=on_progress)

    def unload(self) -> None:
        """Release the model from VRAM and clear the CUDA cache."""
        ocr_core.unload_model()


# ── Dependency factory ────────────────────────────────────────────────────────

_model_service: ModelService | None = None

def get_model_service() -> ModelService:
    """FastAPI Depends() factory — returns the module-level singleton."""
    global _model_service
    if _model_service is None:
        _model_service = ModelService()
    return _model_service
