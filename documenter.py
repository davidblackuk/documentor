#!/usr/bin/env python3
"""
documenter.py — Legacy terminal UI (kept for headless / scripted use).

The OCR engine has moved to ocr_core.py.  This file re-exports the symbols
it used to own so any external scripts that imported from here still work,
and provides the original Rich-based interactive menu as a fallback CLI.

For the full web UI, run start.sh (or: uvicorn server:app) instead.
"""

import shutil, subprocess, sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                            TextColumn, TimeElapsedColumn, TimeRemainingColumn)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
import pyfiglet

# Re-export everything from the engine so legacy importers are unaffected.
from ocr_core import (  # noqa: F401
    BASE_DIR, INPUT_DIR, OUTPUT_DIR, PREVIEW_DIR,
    DEEPSEEK_PATH, ScanEvent,
    load_model, unload_model, is_model_loaded,
    process_pdf, strip_tags, slugify,
)

console = Console()

MODELS = {
    "deepseek": {"name": "DeepSeek-OCR", "installed": DEEPSEEK_PATH.exists()},
    "marker":   {"name": "Marker",        "installed": False},
}
try:
    import marker  # noqa: F401
    MODELS["marker"]["installed"] = True
except ImportError:
    pass

_current_model = "deepseek"


# ── Rich progress callback ─────────────────────────────────────────────────────
# Bridges ocr_core's ScanEvent callback to Rich's Progress bar so the
# legacy CLI gets the same visual feedback as before.

def _make_rich_callback(bar, task):
    """Return a ScanEvent callback that advances a Rich progress bar."""
    def on_event(event: ScanEvent):
        if event.event_type == "page":
            bar.advance(task)
        elif event.event_type == "error":
            console.print(f"\n[red]  {event.message}[/red]")
        elif event.event_type == "log":
            console.print(f"[dim]{event.message}[/dim]")
    return on_event


def _rich_process_pdf(pdf_path, out_dir, page_start=1, page_end=None):
    """Wrap process_pdf with a Rich progress bar for CLI use."""
    import fitz
    doc   = fitz.open(str(pdf_path))
    total = min(page_end or len(doc), len(doc)) - (page_start - 1)
    doc.close()

    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=36),
        MofNCompleteColumn(),
        TextColumn("·"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
    ) as bar:
        task = bar.add_task(pdf_path.name, total=total)
        return process_pdf(
            pdf_path, out_dir,
            page_start=page_start,
            page_end=page_end,
            on_progress=_make_rich_callback(bar, task),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def has_glow() -> bool:
    return shutil.which("glow") is not None


def show_banner():
    console.clear()
    art = pyfiglet.figlet_format("DocuMentor", font="slant")
    console.print(f"[bold cyan]{art}[/bold cyan]", end="")
    console.print(Panel(
        f"[dim]PDF → Markdown Converter  ·  Model: {MODELS[_current_model]['name']}[/dim]",
        border_style="cyan",
        padding=(0, 4),
    ))
    console.print()


# ── Menu actions ───────────────────────────────────────────────────────────────

def action_scan_new():
    show_banner()
    pdfs = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        console.print("[red]No PDFs found in input/[/red]")
        Prompt.ask("\nPress Enter to return")
        return

    new = [p for p in pdfs if not (OUTPUT_DIR / p.stem / f"{p.stem}.md").exists()]
    if not new:
        console.print("[yellow]All PDFs already processed. Use Rescan to reprocess.[/yellow]")
        Prompt.ask("\nPress Enter to return")
        return

    console.print(f"[green]{len(new)} new PDF(s) to process:[/green]")
    for p in new:
        console.print(f"  • {p.name}")
    console.print()

    load_model()
    for pdf in new:
        out_dir = OUTPUT_DIR / pdf.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        console.rule(f"[bold]{pdf.name}[/bold]")
        sections, n_imgs = _rich_process_pdf(pdf, out_dir)
        md = out_dir / f"{pdf.stem}.md"
        md.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
        console.print(
            f"[green]✓  {md.relative_to(BASE_DIR)}"
            f"  ({len(sections)} pages · {n_imgs} images)[/green]"
        )

    console.print("\n[bold green]All done.[/bold green]")
    Prompt.ask("\nPress Enter to return")


def action_rescan():
    show_banner()
    pdfs = {p.stem: p for p in sorted(INPUT_DIR.glob("*.pdf"))}
    if not pdfs:
        console.print("[red]No PDFs in input/[/red]")
        Prompt.ask("\nPress Enter to return")
        return

    t = Table(box=box.ROUNDED)
    t.add_column("#", style="dim", width=4)
    t.add_column("PDF")
    t.add_column("Status")
    stems = sorted(pdfs)
    for i, stem in enumerate(stems, 1):
        done   = (OUTPUT_DIR / stem / f"{stem}.md").exists()
        status = "[green]processed[/green]" if done else "[dim]not yet processed[/dim]"
        t.add_row(str(i), pdfs[stem].name, status)
    console.print(t)

    choice = IntPrompt.ask("\nSelect # (0 = cancel)", default=0)
    if choice < 1 or choice > len(stems):
        return

    stem    = stems[choice - 1]
    pdf     = pdfs[stem]
    out_dir = OUTPUT_DIR / stem

    if out_dir.exists():
        if not Confirm.ask(
            f"Delete existing output for [cyan]{stem}[/cyan] and rescan?", default=False
        ):
            return
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    load_model()
    console.rule(f"[bold]{pdf.name}[/bold]")
    sections, n_imgs = _rich_process_pdf(pdf, out_dir)
    md = out_dir / f"{stem}.md"
    md.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
    console.print(
        f"[green]✓  {md.relative_to(BASE_DIR)}"
        f"  ({len(sections)} pages · {n_imgs} images)[/green]"
    )
    Prompt.ask("\nPress Enter to return")


def action_test_scan():
    show_banner()
    import fitz
    pdfs = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        console.print("[red]No PDFs in input/[/red]")
        Prompt.ask("\nPress Enter to return")
        return

    t = Table(title="Test Scan — select a PDF", box=box.ROUNDED)
    t.add_column("#", style="dim", width=4)
    t.add_column("PDF")
    t.add_column("Pages", justify="right")
    counts = []
    for p in pdfs:
        doc = fitz.open(str(p)); n = len(doc); doc.close()
        counts.append(n)
        t.add_row(str(len(counts)), p.name, str(n))
    console.print(t)

    choice = IntPrompt.ask("\nSelect # (0 = cancel)", default=0)
    if choice < 1 or choice > len(pdfs):
        return

    pdf   = pdfs[choice - 1]
    total = counts[choice - 1]
    console.print(f"\n[cyan]{pdf.name}[/cyan]  [dim]({total} pages)[/dim]")
    start = IntPrompt.ask("First page", default=1)
    end   = IntPrompt.ask("Last page",  default=min(start + 9, total))
    start = max(1, min(start, total))
    end   = max(start, min(end, total))

    preview_out = PREVIEW_DIR / pdf.stem
    if preview_out.exists():
        shutil.rmtree(preview_out)
    preview_out.mkdir(parents=True, exist_ok=True)

    load_model()
    console.rule(f"[bold]Pages {start}–{end} of {pdf.name}[/bold]")
    sections, n_imgs = _rich_process_pdf(pdf, preview_out, page_start=start, page_end=end)

    out_md = preview_out / f"{pdf.stem}_p{start}-{end}.md"
    out_md.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
    console.print(
        f"[green]✓  {out_md.relative_to(BASE_DIR)}"
        f"  ({len(sections)} pages · {n_imgs} images)[/green]"
    )

    if has_glow() and Confirm.ask("Open preview in Glow?", default=True):
        subprocess.run(["glow", "-p", str(out_md)])

    Prompt.ask("\nPress Enter to return")


def action_view_glow():
    show_banner()
    if not has_glow():
        console.print("[red]glow not found.[/red]  Install: [cyan]sudo apt install glow[/cyan]")
        Prompt.ask("\nPress Enter to return")
        return

    md_files = []
    for base in (OUTPUT_DIR, PREVIEW_DIR):
        if base.exists():
            md_files.extend(sorted(base.rglob("*.md")))

    if not md_files:
        console.print("[yellow]No markdown files found yet.[/yellow]")
        Prompt.ask("\nPress Enter to return")
        return

    t = Table(box=box.ROUNDED)
    t.add_column("#", style="dim", width=4)
    t.add_column("File", style="cyan")
    for i, f in enumerate(md_files, 1):
        t.add_row(str(i), str(f.relative_to(BASE_DIR)))
    console.print(t)

    choice = IntPrompt.ask("\nSelect # (0 = cancel)", default=0)
    if choice < 1 or choice > len(md_files):
        return
    subprocess.run(["glow", "-p", str(md_files[choice - 1])])


def action_select_model():
    global _current_model
    show_banner()

    keys = list(MODELS)
    t = Table(title="OCR Models", box=box.ROUNDED)
    t.add_column("#", style="dim", width=4)
    t.add_column("Model", style="cyan")
    t.add_column("Status")
    t.add_column("Active", justify="center")
    for i, key in enumerate(keys, 1):
        m      = MODELS[key]
        status = "[green]installed[/green]" if m["installed"] else "[red]not installed[/red]"
        active = "[bold green]✓[/bold green]" if key == _current_model else ""
        t.add_row(str(i), m["name"], status, active)
    console.print(t)

    choice = IntPrompt.ask("\nSelect # (0 = cancel)", default=0)
    if choice < 1 or choice > len(keys):
        return

    key = keys[choice - 1]
    if not MODELS[key]["installed"]:
        console.print(f"[red]{MODELS[key]['name']} is not installed.[/red]")
    elif key == _current_model:
        console.print(f"[yellow]{MODELS[key]['name']} is already active.[/yellow]")
    else:
        unload_model()
        _current_model = key
        console.print(f"[green]Switched to {MODELS[key]['name']}.[/green]")

    Prompt.ask("\nPress Enter to return")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    for d in (INPUT_DIR, OUTPUT_DIR, PREVIEW_DIR):
        d.mkdir(exist_ok=True)

    while True:
        show_banner()
        glow_note = "" if has_glow() else "  [dim](glow not installed)[/dim]"

        console.print("[bold]Options[/bold]\n")
        console.print("  [cyan]1[/cyan]  Scan new PDFs")
        console.print("  [cyan]2[/cyan]  Rescan a PDF")
        console.print("  [cyan]3[/cyan]  Test scan  [dim](page range → preview/)[/dim]")
        console.print(f"  [cyan]4[/cyan]  Select OCR model  [dim][{MODELS[_current_model]['name']}][/dim]")
        console.print(f"  [cyan]5[/cyan]  View output in Glow{glow_note}")
        console.print("  [cyan]6[/cyan]  Exit\n")

        choice = Prompt.ask("  →", choices=["1","2","3","4","5","6"], default="6")

        match choice:
            case "1": action_scan_new()
            case "2": action_rescan()
            case "3": action_test_scan()
            case "4": action_select_model()
            case "5": action_view_glow()
            case "6":
                console.print("\n[dim]Goodbye.[/dim]\n")
                sys.exit(0)


if __name__ == "__main__":
    main()
