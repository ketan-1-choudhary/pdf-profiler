#!/usr/bin/env python3
"""
render_norm.py — rasterise one PDF page at a NORMALISED size.

Why normalised: the model estimates font sizes from pixels, so pixel size has
to mean the same thing on every page of the book. Rendering each page so its
long edge is exactly TARGET_LONG_EDGE px gives a uniform px-per-pt scale for
any book with a uniform page size, and the scale is written to page_meta.json
either way, so px -> pt conversion is exact rather than assumed.

Emits (into --work-dir):
  page.png        the normalised render, the model's ground truth
  page_meta.json  page size in pt, image size in px, pxPerPt, text-layer flag

Prints page_meta.json to stdout. Exit non-zero only if the page cannot be
rendered at all — everything else degrades to a note.

Usage:
  python3 scripts/render_norm.py --pdf book.pdf --page 12 --work-dir $WORK
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import TARGET_LONG_EDGE, emit, fail, write_json  # noqa: E402


def find_poppler_tool(tool):
    """Resolve a poppler binary without relying on PATH.

    Same resolution order as the pdf-to-html skill's analyze_page.py:
    $POPPLER_PATH, then PATH, then common conda / poppler-for-Windows dirs.
    """
    exe = tool + (".exe" if os.name == "nt" else "")

    env_dir = os.environ.get("POPPLER_PATH")
    if env_dir:
        candidate = os.path.join(env_dir, exe)
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which(tool) or shutil.which(exe)
    if found:
        return found

    candidates = []
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, "miniconda3", "Library", "bin"),
        os.path.join(home, "Miniconda3", "Library", "bin"),
        os.path.join(home, "anaconda3", "Library", "bin"),
        os.path.join(home, "Anaconda3", "Library", "bin"),
        r"C:\ProgramData\miniconda3\Library\bin",
        r"C:\ProgramData\Anaconda3\Library\bin",
    ):
        candidates.append(os.path.join(base, exe))
    for pattern in (r"C:\poppler*\**\bin", r"C:\Program Files\poppler*\**\bin",
                    os.path.join(home, "poppler*", "**", "bin")):
        for directory in glob.glob(pattern, recursive=True):
            candidates.append(os.path.join(directory, exe))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return tool


def pdf_info(pdf_path, page):
    """Page size in points and total page count, via pdfinfo."""
    info = {"pageCountTotal": None, "widthPt": None, "heightPt": None, "notes": []}
    try:
        proc = subprocess.run(
            [find_poppler_tool("pdfinfo"), "-f", str(page), "-l", str(page), pdf_path],
            check=True, capture_output=True, text=True, errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        info["notes"].append(f"pdfinfo unavailable: {exc}")
        return info

    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                info["pageCountTotal"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        # "Page 12 size: 595.276 x 841.89 pts (A4)" or "Page size: ..."
        elif re.match(r"Page\s+\d+\s+size:", line) or line.startswith("Page size:"):
            match = re.search(r"([\d.]+)\s*x\s*([\d.]+)\s*pts", line)
            if match:
                info["widthPt"] = float(match.group(1))
                info["heightPt"] = float(match.group(2))
    return info


def has_text_layer(pdf_path, page):
    """True when this page carries extractable text (not a scan)."""
    try:
        proc = subprocess.run(
            [find_poppler_tool("pdftotext"), "-f", str(page), "-l", str(page),
             pdf_path, "-"],
            check=True, capture_output=True, text=True, errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return len(proc.stdout.strip()) >= 20


def render(pdf_path, page, work_dir, long_edge, width_pt, height_pt):
    """Render the page so its long edge is `long_edge` px. Returns (path, w, h, dpi)."""
    os.makedirs(work_dir, exist_ok=True)
    out_path = os.path.join(work_dir, "page.png")

    # Pick the dpi that lands the long edge on target; pdftoppm takes integer
    # dpi, so the exact size is enforced by a final resize.
    if width_pt and height_pt:
        dpi = max(36, min(600, int(round(72.0 * long_edge / max(width_pt, height_pt)))))
    else:
        dpi = 150

    with tempfile.TemporaryDirectory() as tmp_dir:
        stem = os.path.join(tmp_dir, "p")
        try:
            subprocess.run(
                [find_poppler_tool("pdftoppm"), "-r", str(dpi), "-f", str(page),
                 "-l", str(page), "-png", pdf_path, stem],
                check=True, capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            fail("pdftoppm failed to render the page",
                 detail=detail.decode("utf-8", "replace")[:400], page=page)

        rendered = next((p for p in (f"{stem}-{page}.png", f"{stem}-{page:02d}.png",
                                     f"{stem}-{page:03d}.png", f"{stem}-{page:04d}.png")
                         if os.path.exists(p)), None)
        if rendered is None:
            matches = sorted(glob.glob(stem + "-*.png"))
            rendered = matches[0] if matches else None
        if rendered is None:
            fail("pdftoppm produced no output", page=page)

        image = Image.open(rendered).convert("RGB")
        current_long = max(image.size)
        if current_long != long_edge:
            ratio = long_edge / float(current_long)
            image = image.resize(
                (max(1, round(image.size[0] * ratio)), max(1, round(image.size[1] * ratio))),
                Image.LANCZOS,
            )
        image.save(out_path, optimize=True)
        return out_path, image.size[0], image.size[1], dpi


def main():
    parser = argparse.ArgumentParser(description="Normalised single-page render for style profiling")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--long-edge", type=int, default=TARGET_LONG_EDGE,
                        help=f"target long edge in px (default {TARGET_LONG_EDGE})")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        fail("pdf not found", pdf=args.pdf)
    if args.page < 1:
        fail("page must be >= 1", page=args.page)

    info = pdf_info(args.pdf, args.page)
    total = info.get("pageCountTotal")
    if isinstance(total, int) and args.page > total:
        fail("page out of range", page=args.page, pageCountTotal=total)

    png, image_w, image_h, dpi = render(
        args.pdf, args.page, args.work_dir, args.long_edge,
        info.get("widthPt"), info.get("heightPt"),
    )

    width_pt = info.get("widthPt")
    height_pt = info.get("heightPt")
    px_per_pt = None
    if width_pt and height_pt:
        # Uniform in both axes by construction (aspect ratio is preserved).
        px_per_pt = round(image_w / float(width_pt), 6)
    else:
        info["notes"].append("page size unknown; px->pt conversion unavailable")

    meta = {
        "ok": True,
        "page": args.page,
        "pageCountTotal": total,
        "pdfPath": os.path.abspath(args.pdf),
        "widthPt": width_pt,
        "heightPt": height_pt,
        "imageWidth": image_w,
        "imageHeight": image_h,
        "pxPerPt": px_per_pt,
        "renderDpi": dpi,
        "targetLongEdge": args.long_edge,
        "orientation": "landscape" if image_w > image_h else "portrait",
        "hasTextLayer": has_text_layer(args.pdf, args.page),
        "imagePath": os.path.abspath(png),
        "notes": info.get("notes", []),
    }
    write_json(os.path.join(args.work_dir, "page_meta.json"), meta)
    emit(meta)


if __name__ == "__main__":
    main()
