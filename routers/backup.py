"""
routers/backup.py — Manual save-point endpoint for output/.

Lets the dashboard trigger a git commit + push of the output/ repo on
demand, on top of the automatic pre-write snapshot in PdfService.
"""

from fastapi import APIRouter, Depends, HTTPException

from services.pdf_service import PdfService, get_pdf_service

router = APIRouter()


@router.post("/output/save-point")
def save_point(svc: PdfService = Depends(get_pdf_service)):
    """Commit any pending output/ changes and push them to the remote."""
    try:
        return svc.backup_output()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
