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
import subprocess
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
    "scanned"   — output/<stem>/<stem>.md exists and is non-empty
    "cancelled" — a scan was stopped by request; partial progress is kept
    "partial"   — a .partial.md exists (scan in progress or interrupted)
    "pending"   — no output yet
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
            cancelled  = ocr_core.OUTPUT_DIR / stem / ".cancelled"

            if md_path.exists() and md_path.stat().st_size > 0:
                status = "scanned"
            elif cancelled.exists():
                status = "cancelled"
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
        self._snapshot(f"Auto-snapshot before overwriting {stem}")
        out_dir = self.get_output_dir(stem)
        (out_dir / f"{stem}.md").write_text(content, encoding="utf-8")

    def mark_cancelled(self, stem: str) -> None:
        """Flag a document as cancelled so list_pdfs reports it distinctly from 'partial'."""
        out_dir = self.get_output_dir(stem)
        (out_dir / ".cancelled").touch()

    def clear_cancelled(self, stem: str) -> None:
        """Remove the cancelled marker, e.g. when a fresh scan starts."""
        marker = ocr_core.OUTPUT_DIR / stem / ".cancelled"
        marker.unlink(missing_ok=True)

    def _snapshot(self, message: str) -> None:
        """
        Commit any pending edits in output/ before a scan overwrites them.

        output/ is its own git repo, separate from the app repo, precisely so
        hand-edited markdown survives a rescan. No-ops silently if output/
        isn't a git repo (e.g. not yet initialised) or there's nothing to
        commit — this is a safety net, not something that should ever block
        a scan.
        """
        if not (ocr_core.OUTPUT_DIR / ".git").exists():
            return
        try:
            subprocess.run(
                ["git", "-C", str(ocr_core.OUTPUT_DIR), "add", "-A"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(ocr_core.OUTPUT_DIR), "commit", "-m", message],
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            pass

    def backup_output(self, message: str = "Manual save point") -> dict:
        """
        Commit any outstanding output/ changes and push them to its remote.

        Unlike _snapshot (a silent safety net called on every write), this is
        user-initiated from the dashboard's "Save Point" button, so failures
        are raised rather than swallowed — the user is explicitly asking for
        confirmation the push worked.
        """
        repo = ocr_core.OUTPUT_DIR
        if not (repo / ".git").exists():
            raise RuntimeError("output/ is not a git repository yet")

        add = subprocess.run(
            ["git", "-C", str(repo), "add", "-A"], capture_output=True, text=True,
        )
        if add.returncode != 0:
            raise RuntimeError(add.stderr.strip() or "git add failed")

        commit = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            capture_output=True, text=True,
        )
        committed = commit.returncode == 0

        push = subprocess.run(
            ["git", "-C", str(repo), "push"], capture_output=True, text=True,
        )
        if push.returncode != 0:
            raise RuntimeError(push.stderr.strip() or "git push failed")

        return {"committed": committed, "pushed": True}

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
        # A replacement function (not a string) sidesteps re.sub's backslash
        # escape processing (\1, \g<name>, ...) entirely, so new_text is
        # inserted verbatim regardless of what characters it contains.
        updated = re.sub(
            pattern,
            lambda m: f"{m.group(1)}\n\n{new_text}\n\n",
            content,
            count=1,
            flags=re.DOTALL,
        )

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

    # ── Version history ───────────────────────────────────────────────────────

    def list_versions(self, stem: str) -> list[dict]:
        """
        Return committed snapshots of this document's markdown, newest first.

        Each entry is a prior state of output/<stem>/<stem>.md captured by
        _snapshot() (auto, before every overwrite) or backup_output() (manual,
        user-triggered push). The current on-disk content is *not* included
        here — it's already available via get_markdown() and may not be
        committed yet.
        """
        repo = ocr_core.OUTPUT_DIR
        if not (repo / ".git").exists():
            return []

        rel = f"{stem}/{stem}.md"
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H|%aI|%s", "--", rel],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []

        versions = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            commit_hash, date, message = line.split("|", 2)
            versions.append({"hash": commit_hash, "date": date, "message": message})
        return versions

    def get_markdown_at_revision(self, stem: str, rev: str) -> str:
        """Return the markdown content for a document as of a prior commit."""
        repo = ocr_core.OUTPUT_DIR
        rel  = f"{stem}/{stem}.md"
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{rev}:{rel}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"Revision '{rev}' not found for '{stem}'")
        return result.stdout

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
