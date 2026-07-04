#!/usr/bin/env python3
"""DocuMentor — PDF to Markdown Converter"""

import io, os, re, shutil, subprocess, sys, tempfile, warnings
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

os.environ["TRANSFORMERS_VERBOSITY"] = "error"
warnings.filterwarnings("ignore")

import fitz
import torch
import pyfiglet
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                            TextColumn, TimeElapsedColumn, TimeRemainingColumn)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

console = Console()

BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
PREVIEW_DIR = BASE_DIR / "preview"

BLOG_DIR        = Path("/home/davidb/Documents/Git/davidblackuk.github.io")
BLOG_POSTS_DIR  = BLOG_DIR / "_posts"
BLOG_IMAGES_DIR = BLOG_DIR / "images" / "atari"

DEEPSEEK_PATH = Path(
    "/home/davidb/Documents/AI/ComfyUI/models/deepseek-ocr/deepseek-ai_DeepSeek-OCR"
)

MODELS = {
    "deepseek": {"name": "DeepSeek-OCR", "installed": DEEPSEEK_PATH.exists()},
    "marker":   {"name": "Marker",        "installed": False},
}
try:
    import marker  # noqa: F401
    MODELS["marker"]["installed"] = True
except ImportError:
    pass

_model            = None
_tokenizer        = None
_current_model    = "deepseek"

OCR_PROMPT = "<image>\n<|grounding|>Convert the document to markdown. "


# ── Helpers ──────────────────────────────────────────────────────────────────

def strip_tags(text: str) -> str:
    text = re.sub(r"<\|ref\|>.*?<\|/ref\|>", "", text)
    text = re.sub(r"<\|det\|>.*?<\|/det\|>", "", text)
    return text.strip()


def slugify(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


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


# ── Model management ─────────────────────────────────────────────────────────

def load_model():
    global _model, _tokenizer
    if _model is not None:
        return
    if _current_model == "deepseek":
        from transformers import AutoModel, AutoTokenizer
        with console.status("[yellow]Loading DeepSeek-OCR…[/yellow]", spinner="dots"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                _tokenizer = AutoTokenizer.from_pretrained(
                    str(DEEPSEEK_PATH), trust_remote_code=True
                )
                _model = AutoModel.from_pretrained(
                    str(DEEPSEEK_PATH),
                    _attn_implementation="eager",
                    trust_remote_code=True,
                    use_safetensors=True,
                )
                _model = _model.eval().cuda().to(torch.bfloat16)
        console.print("[green]✓ Model ready[/green]\n")
    else:
        console.print("[red]Selected model not yet implemented.[/red]")


def unload_model():
    global _model, _tokenizer
    if _model is not None:
        del _model, _tokenizer
        _model = _tokenizer = None
        torch.cuda.empty_cache()


# ── Core OCR ─────────────────────────────────────────────────────────────────

def ocr_page(img_path: str, page_tmp: Path, page_num: int,
             imgs_dir: Path, img_counter: int) -> tuple[str, int]:
    page_tmp.mkdir(exist_ok=True)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        _model.infer(
            _tokenizer,
            prompt=OCR_PROMPT,
            image_file=img_path,
            output_path=str(page_tmp),
            base_size=1024,
            image_size=640,
            crop_mode=True,
            save_results=True,
            test_compress=True,
        )
    torch.cuda.empty_cache()

    mmd = page_tmp / "result.mmd"
    text = mmd.read_text(encoding="utf-8") if mmd.exists() else "[NO OUTPUT]"

    src_imgs = page_tmp / "images"
    if src_imgs.is_dir():
        for fname in sorted(os.listdir(src_imgs)):
            ext  = Path(fname).suffix
            dest = f"p{page_num:04d}_{img_counter:04d}{ext}"
            shutil.copy2(src_imgs / fname, imgs_dir / dest)
            text = text.replace(f"images/{fname}", f"images/{dest}")
            img_counter += 1

    return strip_tags(text), img_counter


def process_pdf(pdf_path: Path, out_dir: Path,
                page_start: int = 1, page_end: int = None) -> tuple[list, int]:
    imgs_dir = out_dir / "images"
    imgs_dir.mkdir(parents=True, exist_ok=True)

    doc   = fitz.open(str(pdf_path))
    total = len(doc)
    page_end = min(page_end or total, total)
    pages    = range(page_start - 1, page_end)

    sections    = []
    img_counter = 0
    partial_md  = out_dir / f"{out_dir.name}.partial.md"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Render all pages to PNG first (fast)
        page_imgs = []
        for i in pages:
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = doc[i].get_pixmap(matrix=mat)
            p   = tmp / f"page_{i+1:04d}.png"
            pix.save(str(p))
            page_imgs.append((i + 1, str(p)))
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
            task = bar.add_task(pdf_path.name, total=len(page_imgs))
            for page_num, img_path in page_imgs:
                page_tmp = tmp / f"ocr_{page_num:04d}"
                try:
                    text, img_counter = ocr_page(
                        img_path, page_tmp, page_num, imgs_dir, img_counter
                    )
                except Exception as exc:
                    torch.cuda.empty_cache()
                    text = f"[OCR ERROR page {page_num}: {exc}]"
                    console.print(f"\n[red]  Page {page_num} failed: {exc}[/red]")
                sections.append(f"<!-- page {page_num} -->\n\n{text}")
                partial_md.write_text(
                    "\n\n---\n\n".join(sections), encoding="utf-8"
                )
                bar.advance(task)

    if partial_md.exists():
        partial_md.unlink()

    return sections, img_counter


# ── Menu actions ──────────────────────────────────────────────────────────────

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
        sections, n_imgs = process_pdf(pdf, out_dir)
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
    sections, n_imgs = process_pdf(pdf, out_dir)
    md = out_dir / f"{stem}.md"
    md.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
    console.print(
        f"[green]✓  {md.relative_to(BASE_DIR)}"
        f"  ({len(sections)} pages · {n_imgs} images)[/green]"
    )
    Prompt.ask("\nPress Enter to return")


def action_test_scan():
    show_banner()
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
    sections, n_imgs = process_pdf(pdf, preview_out, page_start=start, page_end=end)

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


def action_publish_to_blog():
    from datetime import date
    show_banner()

    docs = [d for d in sorted(OUTPUT_DIR.iterdir()) if d.is_dir()]
    if not docs:
        console.print("[yellow]No processed documents found in output/[/yellow]")
        Prompt.ask("\nPress Enter to return")
        return

    t = Table(title="Publish to Blog", box=box.ROUNDED)
    t.add_column("#", style="dim", width=4)
    t.add_column("Document", style="cyan")
    t.add_column("Markdown")
    for i, doc_dir in enumerate(docs, 1):
        md = doc_dir / f"{doc_dir.name}.md"
        status = "[green]ready[/green]" if md.exists() else "[red]missing[/red]"
        t.add_row(str(i), doc_dir.name, status)
    console.print(t)

    choice = IntPrompt.ask("\nSelect # (0 = cancel)", default=0)
    if choice < 1 or choice > len(docs):
        return

    doc_dir = docs[choice - 1]
    md_src  = doc_dir / f"{doc_dir.name}.md"
    if not md_src.exists():
        console.print(f"[red]Markdown not found: {md_src}[/red]")
        Prompt.ask("\nPress Enter to return")
        return

    post_date = Prompt.ask("Post date", default=date.today().strftime("%Y-%m-%d"))
    slug      = slugify(doc_dir.name)

    dest_md         = BLOG_POSTS_DIR / f"{post_date}-{slug}.md"
    dest_images_dir = BLOG_IMAGES_DIR / slug

    if dest_md.exists():
        if not Confirm.ask(
            f"[yellow]{dest_md.name}[/yellow] already exists. Overwrite?", default=False
        ):
            return

    src_images = doc_dir / "images"
    if src_images.is_dir():
        dest_images_dir.mkdir(parents=True, exist_ok=True)
        for img in sorted(src_images.iterdir()):
            shutil.copy2(img, dest_images_dir / img.name)
        console.print(f"[green]✓ Copied images → images/atari/{slug}/[/green]")

    content = md_src.read_text(encoding="utf-8")
    content = content.replace("](images/", f"](/images/atari/{slug}/")

    front_matter = (
        f'---\n'
        f'layout: post\n'
        f'title: "{doc_dir.name}"\n'
        f'tags: [atari, atari-scanned-doc]\n'
        f'description: "Scanned and OCR\'d: {doc_dir.name}"\n'
        f'date: {post_date}\n'
        f'---\n\n'
    )
    dest_md.write_text(front_matter + content, encoding="utf-8")
    console.print(f"[green]✓ Published → _posts/{dest_md.name}[/green]")
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
        console.print("  [cyan]6[/cyan]  Publish to blog")
        console.print("  [cyan]7[/cyan]  Exit\n")

        choice = Prompt.ask("  →", choices=["1","2","3","4","5","6","7"], default="7")

        match choice:
            case "1": action_scan_new()
            case "2": action_rescan()
            case "3": action_test_scan()
            case "4": action_select_model()
            case "5": action_view_glow()
            case "6": action_publish_to_blog()
            case "7":
                console.print("\n[dim]Goodbye.[/dim]\n")
                sys.exit(0)


if __name__ == "__main__":
    main()
