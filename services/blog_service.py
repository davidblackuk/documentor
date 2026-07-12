"""
services/blog_service.py — Publish a scanned document to the Jekyll blog.

Copies a processed document's markdown + images out of output/ and into
the davidblackuk.github.io repo as a dated blog post, rewriting image
references to match the blog's asset layout. Pulled forward from the
original terminal UI's action_publish_to_blog (documenter.py, pre-web-app).
"""

import shutil
from datetime import date
from pathlib import Path

import ocr_core

BLOG_DIR        = Path("/home/davidb/Documents/Git/davidblackuk.github.io")
BLOG_POSTS_DIR  = BLOG_DIR / "_posts"
BLOG_IMAGES_DIR = BLOG_DIR / "images" / "atari"


class BlogService:
    """Copies output/<stem> into the blog repo as a front-matter'd post."""

    def publish(self, stem: str, post_date: str | None = None, overwrite: bool = False) -> dict:
        """
        Publish output/<stem>/<stem>.md as a dated post in BLOG_POSTS_DIR.

        Raises FileNotFoundError if the document hasn't been scanned yet,
        and FileExistsError if the destination post already exists and
        overwrite is False.
        """
        md_src = ocr_core.OUTPUT_DIR / stem / f"{stem}.md"
        if not md_src.exists():
            raise FileNotFoundError(f"Markdown not found: {md_src}")

        post_date = post_date or date.today().strftime("%Y-%m-%d")
        slug      = ocr_core.slugify(stem)

        dest_md         = BLOG_POSTS_DIR / f"{post_date}-{slug}.md"
        dest_images_dir = BLOG_IMAGES_DIR / slug

        if dest_md.exists() and not overwrite:
            raise FileExistsError(f"Post already exists: {dest_md.name}")

        src_images   = ocr_core.OUTPUT_DIR / stem / "images"
        copied_images = 0
        if src_images.is_dir():
            dest_images_dir.mkdir(parents=True, exist_ok=True)
            for img in sorted(src_images.iterdir()):
                shutil.copy2(img, dest_images_dir / img.name)
                copied_images += 1

        content = md_src.read_text(encoding="utf-8")
        content = content.replace("](images/", f"](/images/atari/{slug}/")

        front_matter = (
            f'---\n'
            f'layout: post\n'
            f'title: "{stem}"\n'
            f'tags: [atari, atari-scanned-doc, retro]\n'
            f'description: "Scanned and OCR\'d: {stem}"\n'
            f'date: {post_date}\n'
            f'---\n\n'
        )
        BLOG_POSTS_DIR.mkdir(parents=True, exist_ok=True)
        dest_md.write_text(front_matter + content, encoding="utf-8")

        return {
            "post": str(dest_md.relative_to(BLOG_DIR)),
            "images_copied": copied_images,
        }


# ── Dependency factory ────────────────────────────────────────────────────────

_blog_service: BlogService | None = None

def get_blog_service() -> BlogService:
    """FastAPI Depends() factory — returns the module-level singleton."""
    global _blog_service
    if _blog_service is None:
        _blog_service = BlogService()
    return _blog_service
