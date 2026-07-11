"""
routers/formatting.py — Code block reformatting endpoint.

Used by the editor's "Code Block" toolbar action: format a selected snippet
before wrapping it in a fenced ```lang block. Stateless, so it calls
services.format_service directly rather than going through Depends().
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import format_service

router = APIRouter()


class FormatBody(BaseModel):
    code: str
    language: str


@router.post("/format-code")
def format_code(body: FormatBody):
    try:
        formatted = format_service.format_code(body.code, body.language)
    except format_service.FormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"formatted": formatted}
