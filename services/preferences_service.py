"""
services/preferences_service.py — UI preference storage.

Holds what used to live in the browser's localStorage: theme, editor pane
order, and per-document font sizes / last-read page. Persisted server-side
in a single JSON file so preferences follow the document instead of being
forked per browser/origin.
"""

import json
import threading
from pathlib import Path

import ocr_core

DEFAULT_PANE_ORDER = ["pane-editor", "pane-preview", "pane-pdf"]


def _defaults() -> dict:
    return {"theme": "dark", "paneOrder": list(DEFAULT_PANE_ORDER), "documents": {}}


class PreferencesService:
    """
    Reads/writes preferences.json as a whole. Traffic is low (a handful of
    debounced writes per editing session) so a lock around plain
    read-modify-write is sufficient — no need for finer-grained storage.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or ocr_core.PREFS_PATH
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _defaults()
        defaults = _defaults()
        defaults.update(data)
        return defaults

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_all(self) -> dict:
        with self._lock:
            return self._load()

    def set_theme(self, theme: str) -> dict:
        with self._lock:
            data = self._load()
            data["theme"] = "light" if theme == "light" else "dark"
            self._save(data)
            return data

    def set_pane_order(self, order: list[str]) -> dict:
        with self._lock:
            data = self._load()
            data["paneOrder"] = order
            self._save(data)
            return data

    def update_document(
        self,
        stem: str,
        page: int | None = None,
        editor_font: int | None = None,
        preview_font: int | None = None,
    ) -> dict:
        """Merge the given fields into this document's stored preferences.
        Fields left as None are untouched."""
        with self._lock:
            data = self._load()
            doc = data["documents"].setdefault(stem, {})
            if page is not None:
                doc["page"] = page
            if editor_font is not None:
                doc["editorFont"] = editor_font
            if preview_font is not None:
                doc["previewFont"] = preview_font
            self._save(data)
            return data


# ── Dependency factory ────────────────────────────────────────────────────────

_preferences_service: PreferencesService | None = None

def get_preferences_service() -> PreferencesService:
    """FastAPI Depends() factory — returns the module-level singleton."""
    global _preferences_service
    if _preferences_service is None:
        _preferences_service = PreferencesService()
    return _preferences_service
