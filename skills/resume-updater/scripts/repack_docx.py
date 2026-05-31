"""Repack an unpacked .docx directory back into a .docx file.

Usage:
    python repack_docx.py <unpacked_dir> <output.docx>

Replaces the output file atomically via a .tmp intermediate.
Works on Python 3.9+ (unlike pack.py which requires 3.10+).
"""
import sys
import zipfile
import os
import shutil


def repack(src: str, out: str) -> None:
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, src).replace(os.sep, "/")
                zf.write(filepath, arcname)
    shutil.move(tmp, out)
    print(f"Repacked {src} → {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python repack_docx.py <unpacked_dir> <output.docx>")
        sys.exit(1)
    repack(sys.argv[1], sys.argv[2])
