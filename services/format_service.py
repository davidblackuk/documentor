"""
services/format_service.py — Reformat a code snippet for a chosen language.

Single responsibility: turn raw (often OCR'd, inconsistently indented) code
text into consistently formatted text for a supported language. No markdown
fencing, no HTTP — that's the router's job.

Supported languages
--------------------
"c", "cpp", "csharp"  — shelled out to astyle (must be installed: `apt install astyle`)
"basic"               — custom block indenter (no established BASIC formatter exists)
"z80", "m68k"         — custom assembly column-aligner (label / mnemonic / operands / comment)

None of these formatters change identifier, mnemonic, or keyword casing —
only whitespace, indentation, and column alignment. Assembly and BASIC have
no canonical "correct" style, so this sticks to the least surprising option:
reflow whitespace, leave everything else untouched.
"""

import re
import shutil
import subprocess

ASTYLE_LANGUAGES = {"c", "cpp", "csharp"}
ASM_LANGUAGES    = {"z80", "m68k"}
SUPPORTED_LANGUAGES = ASTYLE_LANGUAGES | ASM_LANGUAGES | {"basic"}


class FormatError(Exception):
    """Raised when formatting can't be performed (missing tool, bad input)."""


def format_code(code: str, language: str) -> str:
    language = language.lower()
    if language in ASTYLE_LANGUAGES:
        return _format_with_astyle(code, language)
    if language == "basic":
        return _format_basic(code)
    if language in ASM_LANGUAGES:
        return _format_asm(code)
    raise FormatError(f"Unsupported language: {language}")


# ── astyle (C / C++ / C#) ────────────────────────────────────────────────────

_ASTYLE_MODE = {"c": "c", "cpp": "c", "csharp": "cs"}


def _format_with_astyle(code: str, language: str) -> str:
    if shutil.which("astyle") is None:
        raise FormatError(
            "astyle is not installed. Install it with: sudo apt install astyle"
        )
    # astyle silently skips reformatting the final line if stdin doesn't end
    # in a newline (e.g. "int main(){return 0;}" comes back unformatted).
    if not code.endswith("\n"):
        code += "\n"
    result = subprocess.run(
        ["astyle", f"--mode={_ASTYLE_MODE[language]}", "--style=allman"],
        input=code,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise FormatError(result.stderr.strip() or "astyle failed")
    return result.stdout


# ── BASIC ─────────────────────────────────────────────────────────────────────

_BASIC_INDENT = "    "

_BASIC_OPEN  = re.compile(r"^(FOR|WHILE|DO|SUB|FUNCTION|TYPE|SELECT\s+CASE)\b", re.I)
_BASIC_CLOSE = re.compile(
    r"^(NEXT|WEND|LOOP|END\s+SUB|END\s+FUNCTION|END\s+TYPE|END\s+SELECT|ENDIF|END\s+IF)\b", re.I
)
_BASIC_MID   = re.compile(r"^(ELSE|ELSEIF)\b", re.I)
_BASIC_CASE  = re.compile(r"^CASE\b", re.I)
# A block IF has nothing but a comment after THEN; a single-line IF has a
# statement after THEN and never opens a block.
_BASIC_BLOCK_IF = re.compile(r"^IF\b.*\bTHEN\s*('.*)?$", re.I)
_BASIC_LINE_NUM = re.compile(r"^(\d+)(\s+)(.*)$")


def _format_basic(code: str) -> str:
    out_lines = []
    depth = 0
    for raw_line in code.splitlines():
        line_num_prefix = ""
        stripped = raw_line.strip()

        m = _BASIC_LINE_NUM.match(stripped)
        if m:
            line_num_prefix = f"{m.group(1)} "
            stripped = m.group(3)

        if not stripped:
            out_lines.append("")
            continue

        this_depth = depth
        if _BASIC_CLOSE.match(stripped):
            this_depth = max(0, depth - 1)
            depth = this_depth
        elif _BASIC_MID.match(stripped) or (_BASIC_CASE.match(stripped) and depth > 0):
            this_depth = max(0, depth - 1)
        elif _BASIC_OPEN.match(stripped) or _BASIC_BLOCK_IF.match(stripped):
            depth += 1

        out_lines.append(f"{line_num_prefix}{_BASIC_INDENT * this_depth}{stripped}")

    return "\n".join(out_lines)


# ── Assembly (Z80 / MC68000) ─────────────────────────────────────────────────
#
# Dialect-agnostic column aligner: assumes a `;` line comment and the usual
# convention that a label starts in column 1 (no leading whitespace) while
# instructions are indented. Column widths are fixed tab stops, matching
# classic assembler listing style.

_LABEL_COL   = 0
_MNEMONIC_COL = 8
_OPERAND_COL  = 16
_COMMENT_COL  = 40


def _split_comment(line: str) -> tuple[str, str]:
    """Split a line into (code, comment) at the first un-quoted ';'."""
    in_quote = None
    for i, ch in enumerate(line):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == ";":
            return line[:i], line[i:]
    return line, ""


def _format_asm(code: str) -> str:
    out_lines = []
    for raw_line in code.splitlines():
        if not raw_line.strip():
            out_lines.append("")
            continue

        code_part, comment = _split_comment(raw_line)

        if not code_part.strip():
            # Comment-only line — leave untouched (banners, section headers).
            out_lines.append(raw_line)
            continue

        # OCR'd source loses leading whitespace unreliably, so column
        # position can't distinguish a label from a bare mnemonic (e.g. a
        # zero-operand instruction like RET at column 0 looks identical to
        # a label). Colon suffix is the one unambiguous signal, so only a
        # `label:`-style token is treated as a label; a colonless label
        # followed by code on the same line is (rarely) mis-split as a
        # mnemonic instead.
        tokens = code_part.split()
        had_label = bool(tokens) and tokens[0].endswith(":")
        label = tokens[0] if had_label else ""
        rest  = tokens[1:] if had_label else tokens

        mnemonic = rest[0] if rest else ""
        operands = " ".join(rest[1:])

        pos = _LABEL_COL
        segment = " " * _LABEL_COL
        if label:
            segment = label
            pos = len(segment)

        if mnemonic:
            segment += " " * max(1, _MNEMONIC_COL - pos) + mnemonic
            pos = len(segment)

        if operands:
            segment += " " * max(1, _OPERAND_COL - pos) + operands
            pos = len(segment)

        if comment:
            segment += " " * max(1, _COMMENT_COL - pos) + comment

        out_lines.append(segment.rstrip())

    return "\n".join(out_lines)
