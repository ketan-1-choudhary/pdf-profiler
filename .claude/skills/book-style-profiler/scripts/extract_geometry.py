#!/usr/bin/env python3
"""
extract_geometry.py — measure a normalised page render, visually.

This is the "geometry" half of the model's input. It is deliberately
parser-free: everything below is measured off the rendered pixels, so a
scanned page and a native page are treated identically and an unreliable
structure parser can never poison the profile. Font metrics from a text layer
are attached only as an optional, clearly-flagged enrichment — numbers only,
never text content.

What it measures:
  * page background colour and the printed content box
  * column grid (gutter detection)
  * text blocks: bbox, line count, line height, band/core heights, a font-size
    estimate, ink colour, alignment, width fraction, page zone
  * panels: large uniform non-background fills (what makes a callout a callout)
  * rules: thin high-aspect marks (dividers, underlines, accent bars)
  * image-like regions: cells with high colour diversity
  * optional per-block font metrics (size in pt, family, bold/italic) from
    pdfplumber when a text layer exists

Emits <work-dir>/geometry.json and prints it to stdout. Every sub-step is
fail-soft: a failure adds a note and empties that one field.

Usage:
  python3 scripts/extract_geometry.py --work-dir $WORK [--pdf book.pdf --page 12]
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import (  # noqa: E402
    colours_are_close, delta_e76, emit, fail, parse_hex, read_json,
    srgb_to_lab, to_hex, write_json,
)

INK_DISTANCE = 32          # RGB euclidean distance from background that counts as ink
PANEL_SCALE = 4            # panel/colour analysis runs on a 1/4 downscale
MIN_PANEL_AREA = 0.004     # of page area
MIN_COLUMN_WIDTH = 0.12    # of content width
GUTTER_WIDTH = 0.015       # of content width
X_HEIGHT_RATIO = 0.52      # x-height / em for typical text faces


# ---------------------------------------------------------------------------
# Background + ink
# ---------------------------------------------------------------------------

def dominant_colour(pixels):
    """Modal colour of an (N, 3) array, found on a coarse grid then refined."""
    if pixels.size == 0:
        return (255, 255, 255)
    coarse = (pixels // 16).astype(np.int32)
    keys = coarse[:, 0] * 1024 + coarse[:, 1] * 32 + coarse[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    winner = values[int(np.argmax(counts))]
    mask = keys == winner
    return tuple(int(round(v)) for v in pixels[mask].mean(axis=0))


def page_background(arr):
    """Background from a border ring — page furniture never fills the margins."""
    h, w = arr.shape[:2]
    band_y = max(2, int(h * 0.02))
    band_x = max(2, int(w * 0.02))
    ring = np.concatenate([
        arr[:band_y, :, :].reshape(-1, 3),
        arr[-band_y:, :, :].reshape(-1, 3),
        arr[:, :band_x, :].reshape(-1, 3),
        arr[:, -band_x:, :].reshape(-1, 3),
    ])
    return dominant_colour(ring)


def ink_mask(arr, background, threshold=INK_DISTANCE):
    diff = arr.astype(np.int32) - np.asarray(background, dtype=np.int32)
    return (diff * diff).sum(axis=2) > (threshold * threshold)


def bbox_of(mask):
    rows = mask.any(axis=1)
    cols = mask.any(axis=0)
    if not rows.any() or not cols.any():
        return None
    y0, y1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
    x0, x1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
    return x0, y0, x1, y1


def runs_of(flags, min_length=1):
    """[(start, end_exclusive), ...] for each True run in a 1-D bool array."""
    out = []
    start = None
    for index, value in enumerate(flags):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_length:
                out.append((start, index))
            start = None
    if start is not None and len(flags) - start >= min_length:
        out.append((start, len(flags)))
    return out


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def detect_columns(ink, content):
    """Split the content box on vertical gutters. Returns [(x0, x1), ...]."""
    x0, y0, x1, y1 = content
    region = ink[y0:y1, x0:x1]
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return [(x0, x1)]

    col_ink = region.sum(axis=0)
    quiet = col_ink <= max(1, int(height * 0.004))
    min_gutter = max(6, int(width * GUTTER_WIDTH))

    gutters = [(a, b) for a, b in runs_of(quiet, min_gutter) if a > 0 and b < width]
    if not gutters:
        return [(x0, x1)]

    segments = []
    cursor = 0
    for a, b in gutters:
        if a > cursor:
            segments.append((cursor, a))
        cursor = b
    if cursor < width:
        segments.append((cursor, width))

    segments = [s for s in segments if (s[1] - s[0]) > width * 0.02]
    if not segments:
        return [(x0, x1)]
    # A sliver segment means the "gutter" was really centred-text whitespace,
    # not a column break — fall back to a single column rather than invent one.
    if any((s[1] - s[0]) < width * MIN_COLUMN_WIDTH for s in segments) and len(segments) > 1:
        return [(x0, x1)]
    return [(x0 + a, x0 + b) for a, b in segments]


# ---------------------------------------------------------------------------
# Lines and blocks
# ---------------------------------------------------------------------------

def line_bands(ink, column, y0, y1):
    """Row runs of ink inside one column -> one entry per text line."""
    cx0, cx1 = column
    region = ink[y0:y1, cx0:cx1]
    if region.size == 0:
        return []
    row_ink = region.sum(axis=1)
    active = row_ink > max(1, int((cx1 - cx0) * 0.002))

    bands = []
    for a, b in runs_of(active, 1):
        band = region[a:b]
        cols = band.any(axis=0)
        extent = bbox_of(band)
        if extent is None:
            continue
        bx0, _, bx1, _ = extent
        profile = band.sum(axis=1)
        peak = profile.max() if profile.size else 0
        core = int((profile >= max(1, peak * 0.5)).sum()) if peak else 0
        bands.append({
            "top": y0 + a,
            "bottom": y0 + b,
            "height": b - a,
            "x0": cx0 + bx0,
            "x1": cx0 + bx1,
            "coreHeight": core,
            "inkPixels": int(band.sum()),
            "coverage": float(cols.mean()),
        })

    # Stitch accents/diacritics that split one line into two slivers.
    merged = []
    for band in bands:
        if merged and band["top"] - merged[-1]["bottom"] <= 1:
            previous = merged[-1]
            previous["bottom"] = band["bottom"]
            previous["height"] = previous["bottom"] - previous["top"]
            previous["x0"] = min(previous["x0"], band["x0"])
            previous["x1"] = max(previous["x1"], band["x1"])
            previous["coreHeight"] = max(previous["coreHeight"], band["coreHeight"])
            previous["inkPixels"] += band["inkPixels"]
        else:
            merged.append(band)
    return merged


def font_size_estimate(band):
    """Font size in px from a line band.

    The x-height core (rows carrying at least half the line's peak ink) is the
    most stable signal — band height alone swings with whether the line happens
    to contain a descender. The core estimate is then clamped to a plausible
    multiple of the band height so a stray rule or image row cannot blow it up.
    """
    band_height = float(band["height"])
    core = float(band["coreHeight"])
    if core <= 0:
        return round(band_height / 0.95, 2)
    estimate = core / X_HEIGHT_RATIO
    return round(max(band_height * 0.80, min(estimate, band_height * 1.40)), 2)


def group_bands(bands, column_width):
    """Group line bands into blocks on vertical gap and style discontinuity."""
    if not bands:
        return []

    heights = np.array([b["height"] for b in bands], dtype=float)
    median_height = float(np.median(heights)) or 1.0
    gaps = [bands[i + 1]["top"] - bands[i]["bottom"] for i in range(len(bands) - 1)]
    plausible = [g for g in gaps if 0 <= g <= median_height * 3]
    median_gap = float(np.median(plausible)) if plausible else median_height * 0.4

    blocks = [[bands[0]]]
    for index in range(1, len(bands)):
        previous, current = bands[index - 1], bands[index]
        gap = current["top"] - previous["bottom"]

        size_prev = font_size_estimate(previous)
        size_curr = font_size_estimate(current)
        size_jump = abs(size_curr - size_prev) / max(size_prev, size_curr, 1.0) > 0.25
        indent_jump = abs(current["x0"] - previous["x0"]) > column_width * 0.10
        gap_break = gap > max(median_height * 0.85, median_gap * 1.8 + 1)

        if gap_break or size_jump or indent_jump:
            blocks.append([current])
        else:
            blocks[-1].append(current)
    return blocks


def ink_colour(arr, ink, box):
    """Ink colour of a region: mean of the pixels furthest from the background.

    Averaging every ink pixel drags the result toward the page through
    anti-aliased glyph edges; the top quartile by distance is the real ink.
    """
    x0, y0, x1, y1 = box
    patch = arr[y0:y1, x0:x1].reshape(-1, 3)
    mask = ink[y0:y1, x0:x1].reshape(-1)
    if patch.size == 0 or not mask.any():
        return None
    pixels = patch[mask]
    if len(pixels) > 20000:  # cap the cost on image-heavy blocks
        pixels = pixels[:: max(1, len(pixels) // 20000)]
    luminance = pixels.astype(np.int32).sum(axis=1)
    order = np.argsort(luminance)
    keep = max(1, len(order) // 4)
    # Most books print dark on light; if the page is dark, take the bright tail.
    return tuple(int(round(v)) for v in pixels[order[:keep]].mean(axis=0))


def alignment_of(block_bands, column, column_width):
    cx0, cx1 = column
    x0 = min(b["x0"] for b in block_bands)
    x1 = max(b["x1"] for b in block_bands)
    left_gap = x0 - cx0
    right_gap = cx1 - x1
    tolerance = column_width * 0.04

    if len(block_bands) > 2:
        rights = np.array([b["x1"] for b in block_bands[:-1]], dtype=float)
        lefts = np.array([b["x0"] for b in block_bands[:-1]], dtype=float)
        if rights.std() < column_width * 0.012 and lefts.std() < column_width * 0.012:
            return "justify"

    if abs(left_gap - right_gap) <= tolerance and left_gap > column_width * 0.05:
        return "center"
    if left_gap > right_gap * 3 + tolerance:
        return "right"
    if right_gap > left_gap * 3 + tolerance:
        return "left"
    return "left"


# ---------------------------------------------------------------------------
# Panels, rules, image-like regions
# ---------------------------------------------------------------------------

def label_regions(mask):
    """Connected components of a small bool mask (4-connectivity, iterative)."""
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    current = 0
    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or labels[start_y, start_x]:
                continue
            current += 1
            stack = [(start_y, start_x)]
            labels[start_y, start_x] = current
            while stack:
                y, x = stack.pop()
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] \
                            and not labels[ny, nx]:
                        labels[ny, nx] = current
                        stack.append((ny, nx))
    return labels, current


def detect_panels_and_rules(arr, background, scale=PANEL_SCALE):
    """Find uniform non-background fills (panels) and thin marks (rules).

    Runs on a downscale so the component labelling stays cheap; boxes are
    scaled back to full-resolution pixels.
    """
    height, width = arr.shape[:2]
    small = np.asarray(
        Image.fromarray(arr.astype(np.uint8)).resize(
            (max(1, width // scale), max(1, height // scale)), Image.NEAREST)
    ).astype(np.int32)
    sh, sw = small.shape[:2]
    page_area = float(sh * sw)

    quantised = (small // 24).astype(np.int32)
    keys = quantised[:, :, 0] * 2048 + quantised[:, :, 1] * 64 + quantised[:, :, 2]
    background_key_values, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)

    panels, rules = [], []
    for rank in order[:14]:
        key = int(background_key_values[rank])
        share = counts[rank] / page_area
        if share < 0.0015:
            continue
        mask = keys == key
        colour = dominant_colour(small[mask].reshape(-1, 3))
        if colours_are_close(to_hex(colour), to_hex(background), tolerance=5.0):
            continue

        labels, count = label_regions(mask)
        if count == 0 or count > 600:  # scattered glyph ink, not a panel
            continue
        for component in range(1, count + 1):
            component_mask = labels == component
            area = int(component_mask.sum())
            if area < 6:
                continue
            box = bbox_of(component_mask)
            if box is None:
                continue
            x0, y0, x1, y1 = box
            box_w, box_h = x1 - x0, y1 - y0
            if box_w <= 0 or box_h <= 0:
                continue
            fill_ratio = area / float(box_w * box_h)
            full = {
                "bbox": {"x": x0 * scale, "y": y0 * scale,
                         "width": box_w * scale, "height": box_h * scale},
                "colorHex": to_hex(colour),
                "fillRatio": round(fill_ratio, 3),
            }
            aspect = box_w / float(box_h)
            is_rule = (box_h * scale <= 6 and aspect >= 12) or \
                      (box_w * scale <= 6 and (box_h / float(box_w)) >= 12)
            if is_rule and fill_ratio > 0.6:
                full["orientation"] = "horizontal" if aspect >= 1 else "vertical"
                rules.append(full)
            elif fill_ratio > 0.55 and (box_w * box_h) / page_area >= MIN_PANEL_AREA \
                    and box_w * scale > 40 and box_h * scale > 14:
                panels.append(full)

    panels.sort(key=lambda p: (p["bbox"]["y"], p["bbox"]["x"]))
    rules.sort(key=lambda r: (r["bbox"]["y"], r["bbox"]["x"]))
    return panels[:40], rules[:60]


def detect_image_regions(arr, background, scale=PANEL_SCALE, cell=16):
    """Cells with many distinct colours are photographs/illustrations, not text."""
    height, width = arr.shape[:2]
    small = np.asarray(
        Image.fromarray(arr.astype(np.uint8)).resize(
            (max(1, width // scale), max(1, height // scale)), Image.NEAREST)
    ).astype(np.int32)
    sh, sw = small.shape[:2]
    rows, cols = max(1, sh // cell), max(1, sw // cell)
    flags = np.zeros((rows, cols), dtype=bool)

    quantised = (small // 32).astype(np.int32)
    keys = quantised[:, :, 0] * 512 + quantised[:, :, 1] * 16 + quantised[:, :, 2]
    for row in range(rows):
        for col in range(cols):
            patch = keys[row * cell:(row + 1) * cell, col * cell:(col + 1) * cell]
            if patch.size == 0:
                continue
            if len(np.unique(patch)) >= 7:
                flags[row, col] = True

    labels, count = label_regions(flags)
    regions = []
    for component in range(1, count + 1):
        box = bbox_of(labels == component)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        if (x1 - x0) * (y1 - y0) < 2:
            continue
        regions.append({"bbox": {
            "x": x0 * cell * scale, "y": y0 * cell * scale,
            "width": (x1 - x0) * cell * scale, "height": (y1 - y0) * cell * scale,
        }})
    regions.sort(key=lambda r: (r["bbox"]["y"], r["bbox"]["x"]))
    return regions[:30]


def overlap_fraction(inner, outer):
    ix0, iy0 = inner["x"], inner["y"]
    ix1, iy1 = ix0 + inner["width"], iy0 + inner["height"]
    ox0, oy0 = outer["x"], outer["y"]
    ox1, oy1 = ox0 + outer["width"], oy0 + outer["height"]
    w = max(0, min(ix1, ox1) - max(ix0, ox0))
    h = max(0, min(iy1, oy1) - max(iy0, oy0))
    area = inner["width"] * inner["height"]
    return (w * h) / area if area else 0.0


# ---------------------------------------------------------------------------
# Optional text-layer font metrics (numbers only, no content)
# ---------------------------------------------------------------------------

def font_metrics(pdf_path, page, px_per_pt, blocks, page_height_pt, notes):
    if not pdf_path or not px_per_pt:
        return
    try:
        import pdfplumber
    except ImportError:
        notes.append("pdfplumber not installed; font metrics skipped")
        return
    try:
        with pdfplumber.open(pdf_path) as document:
            if page > len(document.pages):
                return
            chars = document.pages[page - 1].chars
    except Exception as exc:  # noqa: BLE001 - advisory enrichment only
        notes.append(f"font metrics unavailable: {type(exc).__name__}")
        return

    if not chars:
        return

    buckets = {block["id"]: [] for block in blocks}
    for char in chars:
        # pdfplumber y grows downward from the page top in 'top'.
        cx = (float(char.get("x0", 0)) + float(char.get("x1", 0))) / 2.0 * px_per_pt
        cy = (float(char.get("top", 0)) + float(char.get("bottom", 0))) / 2.0 * px_per_pt
        for block in blocks:
            box = block["bbox"]
            if box["x"] - 2 <= cx <= box["x"] + box["width"] + 2 and \
                    box["y"] - 2 <= cy <= box["y"] + box["height"] + 2:
                buckets[block["id"]].append(char)
                break

    for block in blocks:
        collected = buckets.get(block["id"]) or []
        if not collected:
            continue
        sizes = [round(float(c.get("size", 0)), 1) for c in collected if c.get("size")]
        names = {str(c.get("fontname", "")) for c in collected}
        joined = " ".join(names).lower()
        modal = None
        if sizes:
            values, counts = np.unique(np.array(sizes), return_counts=True)
            modal = float(values[int(np.argmax(counts))])
        block["fontMetrics"] = {
            "source": "text-layer",
            "fontSizePt": modal,
            "fontSizeRangePt": [min(sizes), max(sizes)] if sizes else None,
            "fontNames": sorted(n for n in names if n)[:4],
            "boldHint": any(k in joined for k in ("bold", "black", "heavy", "semib")),
            "italicHint": any(k in joined for k in ("italic", "oblique")),
            "monospaceHint": any(k in joined for k in ("mono", "courier", "consol")),
            "serifHint": any(k in joined for k in ("serif", "times", "georgia", "garamond",
                                                   "roman", "book", "minion")),
            "glyphCount": len(collected),
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Visual geometry measurement for style profiling")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--image", default=None, help="defaults to <work-dir>/page.png")
    parser.add_argument("--pdf", default=None, help="enables optional text-layer font metrics")
    parser.add_argument("--page", type=int, default=None)
    args = parser.parse_args()

    meta = read_json(os.path.join(args.work_dir, "page_meta.json"), {}) or {}
    image_path = args.image or meta.get("imagePath") or os.path.join(args.work_dir, "page.png")
    if not os.path.isfile(image_path):
        fail("page render not found; run render_norm.py first", image=image_path)

    page = args.page or meta.get("page")
    px_per_pt = meta.get("pxPerPt")
    notes = []

    arr = np.asarray(Image.open(image_path).convert("RGB")).astype(np.int32)
    height, width = arr.shape[:2]

    background = page_background(arr)
    ink = ink_mask(arr, background)
    content = bbox_of(ink)
    if content is None:
        geometry = {
            "ok": True, "page": page, "blank": True, "imageWidth": width,
            "imageHeight": height, "pxPerPt": px_per_pt,
            "backgroundHex": to_hex(background), "columns": [], "blocks": [],
            "panels": [], "rules": [], "imageRegions": [],
            "notes": ["no ink detected; page treated as blank"],
        }
        write_json(os.path.join(args.work_dir, "geometry.json"), geometry)
        emit(geometry)
        return

    cx0, cy0, cx1, cy1 = content
    content_w, content_h = cx1 - cx0, cy1 - cy0
    columns = detect_columns(ink, content)

    blocks = []
    for column_index, column in enumerate(columns):
        column_width = max(1, column[1] - column[0])
        bands = line_bands(ink, column, cy0, cy1)
        for group in group_bands(bands, column_width):
            x0 = min(b["x0"] for b in group)
            x1 = max(b["x1"] for b in group)
            y0 = min(b["top"] for b in group)
            y1 = max(b["bottom"] for b in group)
            if (x1 - x0) < 2 or (y1 - y0) < 2:
                continue

            tops = [b["top"] for b in group]
            line_height = None
            if len(tops) > 1:
                deltas = np.diff(np.array(tops, dtype=float))
                line_height = round(float(np.median(deltas)), 2)

            sizes = [font_size_estimate(b) for b in group]
            size_estimate = round(float(np.median(np.array(sizes))), 2)
            colour = ink_colour(arr, ink, (x0, y0, x1, y1))
            region_ink = ink[y0:y1, x0:x1]

            centre_y = (y0 + y1) / 2.0
            zone = "header" if centre_y < height * 0.09 else \
                   "footer" if centre_y > height * 0.91 else "body"

            blocks.append({
                "id": f"g{len(blocks) + 1:03d}",
                "column": column_index,
                "zone": zone,
                "bbox": {"x": int(x0), "y": int(y0),
                         "width": int(x1 - x0), "height": int(y1 - y0)},
                "lineCount": len(group),
                "lineHeightPx": line_height,
                "bandHeightPx": round(float(np.median([b["height"] for b in group])), 2),
                "coreHeightPx": round(float(np.median([b["coreHeight"] for b in group])), 2),
                "fontSizePxEstimate": size_estimate,
                "fontSizePtEstimate": round(size_estimate / px_per_pt, 2) if px_per_pt else None,
                "inkColorHex": to_hex(colour) if colour else None,
                "inkCoverage": round(float(region_ink.mean()), 4),
                "textAlignGuess": alignment_of(group, column, column_width),
                "widthFraction": round((x1 - x0) / float(content_w), 3),
                "leftOffsetFraction": round((x0 - column[0]) / float(column_width), 3),
                "indentFraction": round(max(0.0, (group[0]["x0"] - x0) / float(column_width)), 3),
                "approximate": True,
            })

    try:
        panels, rules = detect_panels_and_rules(arr, background)
    except Exception as exc:  # noqa: BLE001
        panels, rules = [], []
        notes.append(f"panel detection failed: {type(exc).__name__}")
    try:
        image_regions = detect_image_regions(arr, background)
    except Exception as exc:  # noqa: BLE001
        image_regions = []
        notes.append(f"image-region detection failed: {type(exc).__name__}")

    # Tie blocks to the panel they sit inside and to overlapping imagery, so the
    # model can tell "boxed callout" from "plain paragraph" without guessing.
    for index, panel in enumerate(panels):
        panel["id"] = f"p{index + 1:02d}"
    for block in blocks:
        block["insidePanelId"] = next(
            (p["id"] for p in panels if overlap_fraction(block["bbox"], p["bbox"]) > 0.75), None)
        block["imageLike"] = any(
            overlap_fraction(block["bbox"], r["bbox"]) > 0.5 for r in image_regions)
        block["nearRuleAbove"] = any(
            r["bbox"]["y"] + r["bbox"]["height"] <= block["bbox"]["y"] + 2 and
            block["bbox"]["y"] - (r["bbox"]["y"] + r["bbox"]["height"]) < block["bandHeightPx"] * 1.2 and
            overlap_fraction({"x": r["bbox"]["x"], "y": block["bbox"]["y"],
                              "width": r["bbox"]["width"], "height": 1}, block["bbox"]) > 0.3
            for r in rules if r.get("orientation") == "horizontal")
        block["nearRuleBelow"] = any(
            r["bbox"]["y"] >= block["bbox"]["y"] + block["bbox"]["height"] - 2 and
            r["bbox"]["y"] - (block["bbox"]["y"] + block["bbox"]["height"]) < block["bandHeightPx"] * 1.2 and
            overlap_fraction({"x": r["bbox"]["x"], "y": block["bbox"]["y"],
                              "width": r["bbox"]["width"], "height": 1}, block["bbox"]) > 0.3
            for r in rules if r.get("orientation") == "horizontal")

    # Vertical rhythm between consecutive body blocks, in ems of each block's
    # own size — the unit the registry stores spacing in.
    body_blocks = [b for b in blocks if b["zone"] == "body"]
    for previous, current in zip(body_blocks, body_blocks[1:]):
        if current["column"] != previous["column"]:
            continue
        gap = current["bbox"]["y"] - (previous["bbox"]["y"] + previous["bbox"]["height"])
        if gap < 0:
            continue
        previous["spaceAfterPx"] = int(gap)
        current["spaceBeforePx"] = int(gap)
        size = current["fontSizePxEstimate"] or 1
        current["spaceBeforeEm"] = round(gap / size, 2)
        previous_size = previous["fontSizePxEstimate"] or 1
        previous["spaceAfterEm"] = round(gap / previous_size, 2)

    # Body text size = the size carrying the most lines. Everything in the
    # registry is sized relative to this, so it must come from mass, not from
    # the first block on the page.
    body_size_px = None
    if body_blocks:
        tally = {}
        for block in body_blocks:
            if block["imageLike"]:
                continue
            key = round(block["fontSizePxEstimate"] / 1.5) * 1.5
            tally[key] = tally.get(key, 0) + block["lineCount"]
        if tally:
            body_size_px = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    if args.pdf and meta.get("hasTextLayer") is not False:
        font_metrics(args.pdf, page, px_per_pt, blocks, meta.get("heightPt"), notes)

    geometry = {
        "ok": True,
        "page": page,
        "blank": False,
        "imageWidth": width,
        "imageHeight": height,
        "pxPerPt": px_per_pt,
        "backgroundHex": to_hex(background),
        "contentBbox": {"x": cx0, "y": cy0, "width": content_w, "height": content_h},
        "marginFractions": {
            "top": round(cy0 / float(height), 4),
            "right": round((width - cx1) / float(width), 4),
            "bottom": round((height - cy1) / float(height), 4),
            "left": round(cx0 / float(width), 4),
        },
        "columnCount": len(columns),
        "columns": [{"index": i, "x0": c[0], "x1": c[1],
                     "widthFraction": round((c[1] - c[0]) / float(content_w), 3)}
                    for i, c in enumerate(columns)],
        "bodyFontSizePxEstimate": body_size_px,
        "bodyFontSizePtEstimate": round(body_size_px / px_per_pt, 2)
        if (body_size_px and px_per_pt) else None,
        "inkCoverage": round(float(ink[cy0:cy1, cx0:cx1].mean()), 4),
        "imageAreaFraction": round(sum(
            r["bbox"]["width"] * r["bbox"]["height"] for r in image_regions) /
            float(width * height), 4),
        "blockCount": len(blocks),
        "blocks": blocks,
        "panels": panels,
        "rules": rules,
        "imageRegions": image_regions,
        "hasTextLayer": meta.get("hasTextLayer"),
        "notes": notes,
    }
    write_json(os.path.join(args.work_dir, "geometry.json"), geometry)
    emit(geometry)


if __name__ == "__main__":
    main()
