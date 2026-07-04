"""
services/pdf_service.py — PDF and Markdown file I/O.

Single responsibility: everything that touches the filesystem for PDFs and
their generated markdown lives here.  No model, no HTTP, no progress events.

The page-marker format used throughout is:

    <!-- page N -->

    … markdown content for page N …

    ---

    <!-- page N+1 -->

This comment-based delimiter is invisible in rendered markdown, survives
round-trips through editors, and lets the web editor locate page boundaries
without parsing the document content.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF — only used for page-count queries

import ocr_core


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PdfInfo:
    """
    Summary record for one PDF in the input directory.

    status values
    -------------
    "scanned"    — output/<stem>/<stem>.md exists and is non-empty
    "partial"    — a .partial.md exists (scan in progress or interrupted)
    "pending"    — no output yet
    """
    stem: str          # filename without extension, used as the document ID
    filename: str      # original filename with .pdf
    status: str        # "scanned" | "partial" | "pending"
    page_count: int    # total pages in the PDF


# ── Service ───────────────────────────────────────────────────────────────────

class PdfService:
    """
    All filesystem operations for PDFs and their markdown output.

    Path resolution is centralised here so routes and other services never
    need to know the directory layout.
    """

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_pdfs(self) -> list[PdfInfo]:
        """Return one PdfInfo per PDF found in input/, sorted by filename."""
        results = []
        for pdf_path in sorted(ocr_core.INPUT_DIR.glob("*.pdf")):
            stem       = pdf_path.stem
            md_path    = ocr_core.OUTPUT_DIR / stem / f"{stem}.md"
            partial    = ocr_core.OUTPUT_DIR / stem / f"{stem}.partial.md"

            if md_path.exists() and md_path.stat().st_size > 0:
                status = "scanned"
            elif partial.exists():
                status = "partial"
            else:
                status = "pending"

            doc        = fitz.open(str(pdf_path))
            page_count = len(doc)
            doc.close()

            results.append(PdfInfo(
                stem=stem,
                filename=pdf_path.name,
                status=status,
                page_count=page_count,
            ))
        return results

    # ── Path resolution ───────────────────────────────────────────────────────

    def get_pdf_path(self, stem: str) -> Path:
        """Resolve the input PDF path for a document stem. Raises if missing."""
        path = ocr_core.INPUT_DIR / f"{stem}.pdf"
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")
        return path

    def get_output_dir(self, stem: str) -> Path:
        """Return (and create if needed) the output directory for a document."""
        d = ocr_core.OUTPUT_DIR / stem
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Markdown I/O ──────────────────────────────────────────────────────────

    def get_markdown(self, stem: str) -> str:
        """
        Read the markdown file for a document.

        Returns the partial file content if a full scan hasn't completed yet,
        so the editor can show progress mid-scan.  Returns an empty string if
        neither file exists.
        """
        md      = ocr_core.OUTPUT_DIR / stem / f"{stem}.md"
        partial = ocr_core.OUTPUT_DIR / stem / f"{stem}.partial.md"

        if md.exists():
            return md.read_text(encoding="utf-8")
        if partial.exists():
            return partial.read_text(encoding="utf-8")
        return ""

    def save_markdown(self, stem: str, content: str) -> None:
        """Write the full markdown file, creating the output directory if needed."""
        out_dir = self.get_output_dir(stem)
        (out_dir / f"{stem}.md").write_text(content, encoding="utf-8")

    def update_page_in_markdown(
        self, stem: str, page_num: int, new_text: str
    ) -> str:
        """
        Replace the content for one page in the existing markdown and save it.

        The function locates the <!-- page N --> marker, replaces everything
        up to the next marker (or end of file), writes the updated content,
        and returns it so the caller can push it back to the client.

        Raises FileNotFoundError if no markdown exists for the document yet.
        """
        content = self.get_markdown(stem)
        if not content:
            raise FileNotFoundError(f"No markdown found for '{stem}'")

        # Match from <!-- page N --> up to the next <!-- page ... --> or EOF.
        # re.DOTALL so '.' matches newlines inside page content.
        pattern = (
            rf"(<!-- page {page_num} -->)"   # keep the marker itself
            rf".*?"                           # existing content (non-greedy)
            rf"(?=<!-- page \d+ -->|$)"       # stop at next marker or EOF
        )
        replacement = rf"\1\n\n{re.escape(new_text)}\n\n"
        updated = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)

        # If the regex matched nothing the marker was missing.  Insert the page
        # at the correct sorted position: just before the first marker whose
        # page number is greater than page_num (or at the end of the file).
        if updated == content:
            insert = f"<!-- page {page_num} -->\n\n{new_text}\n\n"
            pos = len(content)
            for m in re.finditer(r"<!-- page (\d+) -->", content):
                if int(m.group(1)) > page_num:
                    pos = m.start()
                    break
            updated = content[:pos].rstrip("\n") + "\n\n" + insert + content[pos:]

        self.save_markdown(stem, updated)
        return updated

    # ── Page count helper ─────────────────────────────────────────────────────

    def get_page_count(self, stem: str) -> int:
        """Return the total page count of the PDF without keeping it open."""
        doc   = fitz.open(str(self.get_pdf_path(stem)))
        count = len(doc)
        doc.close()
        return count


# ── Dependency factory ────────────────────────────────────────────────────────

_pdf_service: PdfService | None = None

def get_pdf_service() -> PdfService:
    """FastAPI Depends() factory — returns the module-level singleton."""
    global _pdf_service
    if _pdf_service is None:
        _pdf_service = PdfService()
    return _pdf_service
