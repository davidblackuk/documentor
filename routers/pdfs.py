"""
routers/pdfs.py — PDF list, markdown content, and raw PDF file endpoints.

All routes are read/write operations on already-processed documents.
Scanning (which involves the model) lives in routers/scanner.py.
"""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from services.pdf_service import PdfService, get_pdf_service

router = APIRouter()


# ── Request / response models ─────────────────────────────────────────────────

class MarkdownBody(BaseModel):
    content: str


class CropBody(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/pdfs")
def list_pdfs(svc: PdfService = Depends(get_pdf_service)):
    """Return metadata for every PDF in input/, sorted by filename."""
    return [asdict(p) for p in svc.list_pdfs()]


@router.get("/pdf/{stem}/content")
def get_markdown(stem: str, svc: PdfService = Depends(get_pdf_service)):
    """
    Return the markdown content for a document.

    Returns partial content if a scan is still running, or an empty string
    if the document hasn't been scanned yet.
    """
    return {"content": svc.get_markdown(stem)}


@router.get("/pdf/{stem}/versions")
def get_versions(stem: str, svc: PdfService = Depends(get_pdf_service)):
    """Return committed markdown snapshots for a document, newest first."""
    return svc.list_versions(stem)


@router.get("/pdf/{stem}/content/{rev}")
def get_markdown_at_revision(
    stem: str, rev: str, svc: PdfService = Depends(get_pdf_service)
):
    """Return the markdown content for a document as of a prior commit."""
    try:
        return {"content": svc.get_markdown_at_revision(stem, rev)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/pdf/{stem}/content")
def save_markdown(
    stem: str,
    body: MarkdownBody,
    svc: PdfService = Depends(get_pdf_service),
):
    """Persist editor changes back to disk."""
    try:
        svc.save_markdown(stem, body.content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok"}


@router.get("/pdf/{stem}/file")
def get_pdf_file(stem: str, svc: PdfService = Depends(get_pdf_service)):
    """
    Serve the raw PDF for PDF.js to render in the viewer pane.

    FileResponse streams the file with appropriate Content-Type so the
    browser doesn't attempt to save it as a download.
    """
    try:
        path = svc.get_pdf_path(stem)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"PDF '{stem}' not found")
    return FileResponse(str(path), media_type="application/pdf")


@router.get("/pdf/{stem}/page-count")
def get_page_count(stem: str, svc: PdfService = Depends(get_pdf_service)):
    """Return the total number of pages in the PDF."""
    try:
        count = svc.get_page_count(stem)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"PDF '{stem}' not found")
    return {"count": count}


@router.get("/pdf/{stem}/images/{filename}")
def get_image(stem: str, filename: str, svc: PdfService = Depends(get_pdf_service)):
    """
    Serve an extracted figure image from the document's images/ folder.

    The markdown references images as `images/p0001_0000.jpg`; the preview
    pane rewrites those to `/api/pdf/{stem}/images/p0001_0000.jpg` so this
    route resolves them correctly.
    """
    import ocr_core
    path = ocr_core.OUTPUT_DIR / stem / "images" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Image '{filename}' not found")
    return FileResponse(str(path))


@router.post("/pdf/{stem}/crop-image")
def crop_image(
    stem: str,
    body: CropBody,
    svc: PdfService = Depends(get_pdf_service),
):
    """
    Render a dragged rectangle from the PDF pane to a JPEG in images/ and
    return its filename plus a ready-to-paste markdown image link.
    """
    try:
        return svc.save_cropped_image(
            stem, body.page, (body.x0, body.y0, body.x1, body.y1)
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
