"""
routers/blog.py — Publish a scanned document to the Jekyll blog.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.blog_service import BlogService, get_blog_service

router = APIRouter()


class PublishBody(BaseModel):
    post_date: str | None = None
    overwrite: bool = False


@router.post("/pdf/{stem}/publish")
def publish_to_blog(
    stem: str,
    body: PublishBody = PublishBody(),
    svc: BlogService = Depends(get_blog_service),
):
    """Copy the document's markdown + images into the blog repo as a post."""
    try:
        return svc.publish(stem, post_date=body.post_date, overwrite=body.overwrite)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
