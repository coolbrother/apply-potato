#!/usr/bin/env python3
"""
linediff.py — write a markdown file containing ONLY the lines that differ between
two files.

Supports .docx (read in-memory via zipfile — no unpacking, no temp files), and any
text-based format (.md, .txt, .xml, or unknown → treated as plain text).

Usage:
    python linediff.py <file1> <file2> [output_path]

If output_path is omitted, the diff is written to <dir of file2>/diff.md.

Stdlib only — safe to run under any Python on PATH.
"""

import difflib
import html
import re
import sys
import zipfile
from pathlib import Path


# Matches a single <w:t ...>text</w:t> run's inner text
_WT_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)
# Strips any remaining XML tags
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_docx(path: Path) -> list:
    """Extract paragraph text from a .docx, reading the zip in memory."""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")

    lines = []
    # Each paragraph is delimited by </w:p>; collect the <w:t> text within it.
    for para in xml.split("</w:p>"):
        text = "".join(_WT_RE.findall(para))
        text = html.unescape(_TAG_RE.sub("", text)).strip()
        if text:
            lines.append(text)
    return lines


def extract_text(path: Path) -> list:
    """Return a list of logical lines for the given file."""
    if path.suffix.lower() == ".docx":
        return _extract_docx(path)
    # .md, .txt, .xml, or anything else → plain text lines
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def build_diff(file1: Path, file2: Path) -> str:
    """Build the diff.md content containing only the differing lines."""
    a = extract_text(file1)
    b = extract_text(file2)

    # n=0 → no surrounding context, only changed lines (plus +++/---/@@ markers).
    changed = []
    for line in difflib.unified_diff(a, b, n=0):
        if line.startswith(("+++", "---")):
            continue  # file headers
        if line.startswith("@@"):
            continue  # hunk headers
        if line.startswith(("+", "-")):
            changed.append(line.rstrip("\n"))

    header = f"# Diff\n\n`{file1.name}` → `{file2.name}`\n\n"
    if not changed:
        return header + "No differences found.\n"
    return header + "```diff\n" + "\n".join(changed) + "\n```\n"


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python linediff.py <file1> <file2> [output_path]", file=sys.stderr)
        return 2

    file1 = Path(args[0])
    file2 = Path(args[1])
    out = Path(args[2]) if len(args) >= 3 else file2.parent / "diff.md"

    for f in (file1, file2):
        if not f.exists():
            print(f"Error: file not found: {f}", file=sys.stderr)
            return 1

    content = build_diff(file1, file2)
    out.write_text(content, encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
