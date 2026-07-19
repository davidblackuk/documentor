"""
routers/preferences.py — UI preferences: theme, editor pane order, and
per-document font sizes / last-read page. Persisted server-side (see
services/preferences_service.py) instead of the browser's localStorage.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.preferences_service import PreferencesService, get_preferences_service

router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────

class ThemeBody(BaseModel):
    theme: str


class PaneOrderBody(BaseModel):
    order: list[str]


class DocumentPrefsBody(BaseModel):
    page: int | None = None
    editorFont: int | None = None
    previewFont: int | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/preferences")
def get_preferences(svc: PreferencesService = Depends(get_preferences_service)):
    """Return the full preferences blob: theme, paneOrder, and per-document settings."""
    return svc.get_all()


@router.put("/preferences/theme")
def put_theme(body: ThemeBody, svc: PreferencesService = Depends(get_preferences_service)):
    return svc.set_theme(body.theme)


@router.put("/preferences/pane-order")
def put_pane_order(body: PaneOrderBody, svc: PreferencesService = Depends(get_preferences_service)):
    return svc.set_pane_order(body.order)


@router.put("/preferences/document/{stem}")
def put_document_prefs(
    stem: str, body: DocumentPrefsBody, svc: PreferencesService = Depends(get_preferences_service)
):
    return svc.update_document(
        stem, page=body.page, editor_font=body.editorFont, preview_font=body.previewFont
    )
