#!/usr/bin/env python3
"""
prepare_brief.py — assemble the exact, bounded input the model sees for a page.

geometry.json is thorough; a brief is what the model should actually read. This
trims it to the fields that matter for naming a design, caps the block list, and
folds specks into a count — so the per-page prompt is the same shape and roughly
the same size on page 3 and page 300. That bounded, uniform input is most of why
the model's output stays consistent across a long book.

Emits <work-dir>/brief.json and prints a one-line summary.

Usage:
    python3 scripts/prepare_brief.py --work-dir $WORK --profile-json profile.json
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import emit, fail, load_profile, read_json, write_json  # noqa: E402

MIN_BLOCK_AREA = 0.00015  # of page area; below this it is a speck, not a design


def compact_font_metrics(metrics):
    if not isinstance(metrics, dict):
        return None
    hints = [name for name, flag in (
        ("bold", metrics.get("boldHint")), ("italic", metrics.get("italicHint")),
        ("mono", metrics.get("monospaceHint")), ("serif", metrics.get("serifHint")),
    ) if flag]
    out = {"fontSizePt": metrics.get("fontSizePt")}
    if hints:
        out["hints"] = hints
    names = metrics.get("fontNames") or []
    if names:
        out["fontName"] = names[0]
    return out


def main():
    parser = argparse.ArgumentParser(description="Build the bounded per-page model brief")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--profile-json", required=True)
    parser.add_argument("--max-blocks", type=int, default=120)
    args = parser.parse_args()

    meta = read_json(os.path.join(args.work_dir, "page_meta.json"))
    geometry = read_json(os.path.join(args.work_dir, "geometry.json"))
    if not isinstance(meta, dict) or not isinstance(geometry, dict):
        fail("page_meta.json / geometry.json missing; run render_norm.py then "
             "extract_geometry.py first", workDir=args.work_dir)

    try:
        profile = load_profile(args.profile_json, page_count_total=meta.get("pageCountTotal"))
    except ValueError as error:
        fail(str(error), profileJson=args.profile_json)

    page_area = float((geometry.get("imageWidth") or 1) * (geometry.get("imageHeight") or 1))
    blocks = geometry.get("blocks") or []

    kept, minor = [], 0
    for block in blocks:
        box = block.get("bbox") or {}
        area = float(box.get("width", 0) * box.get("height", 0))
        if page_area and area / page_area < MIN_BLOCK_AREA:
            minor += 1
            continue
        entry = {
            "id": block.get("id"),
            "zone": block.get("zone"),
            "col": block.get("column"),
            "bbox": box,
            "lines": block.get("lineCount"),
            "sizePxMeasured": block.get("fontSizePxEstimate"),
            "sizePtMeasured": block.get("fontSizePtEstimate"),
            "inkColorHex": block.get("inkColorHex"),
            "alignGuess": block.get("textAlignGuess"),
            "widthFraction": block.get("widthFraction"),
            "indentFraction": block.get("indentFraction"),
        }
        for key, source in (("lineHeightPx", "lineHeightPx"),
                            ("spaceBeforeEm", "spaceBeforeEm"),
                            ("spaceAfterEm", "spaceAfterEm")):
            if block.get(source) is not None:
                entry[key] = block[source]
        for flag in ("insidePanelId", "imageLike", "nearRuleAbove", "nearRuleBelow"):
            if block.get(flag):
                entry[flag] = block[flag]
        font = compact_font_metrics(block.get("fontMetrics"))
        if font and font.get("fontSizePt"):
            entry["textLayerFont"] = font
        kept.append(entry)

    truncated = 0
    if len(kept) > args.max_blocks:
        # Keep the largest blocks: a dense page's design vocabulary lives in
        # its big elements, and the tail is repetition of what is already here.
        kept.sort(key=lambda b: -(b["bbox"].get("width", 0) * b["bbox"].get("height", 0)))
        truncated = len(kept) - args.max_blocks
        kept = kept[:args.max_blocks]
    kept.sort(key=lambda b: (b["bbox"].get("y", 0), b["bbox"].get("x", 0)))

    brief = {
        "page": meta.get("page"),
        "pageCountTotal": meta.get("pageCountTotal"),
        "image": {
            "path": meta.get("imagePath"),
            "width": meta.get("imageWidth"),
            "height": meta.get("imageHeight"),
            "pxPerPt": meta.get("pxPerPt"),
            "note": "sizes you report in px are converted to pt with pxPerPt; "
                    "the render is normalised so px means the same thing on every page",
        },
        "pageSizePt": {"width": meta.get("widthPt"), "height": meta.get("heightPt")},
        "hasTextLayer": meta.get("hasTextLayer"),
        "blank": bool(geometry.get("blank")),
        "backgroundHex": geometry.get("backgroundHex"),
        "contentBbox": geometry.get("contentBbox"),
        "marginFractions": geometry.get("marginFractions"),
        "columnCount": geometry.get("columnCount"),
        "columns": geometry.get("columns"),
        "bodySizePtMeasured": geometry.get("bodyFontSizePtEstimate"),
        "bodySizePxMeasured": geometry.get("bodyFontSizePxEstimate"),
        "imageAreaFraction": geometry.get("imageAreaFraction"),
        "panels": [{"id": p.get("id"), "bbox": p.get("bbox"), "colorHex": p.get("colorHex")}
                   for p in (geometry.get("panels") or [])],
        "rules": [{"bbox": r.get("bbox"), "colorHex": r.get("colorHex"),
                   "orientation": r.get("orientation")}
                  for r in (geometry.get("rules") or [])[:24]],
        "imageRegions": [r.get("bbox") for r in (geometry.get("imageRegions") or [])],
        "blockCount": len(kept),
        "minorBlocksOmitted": minor,
        "blocksTruncated": truncated,
        "blocks": kept,
        "knownDesigns": [
            {
                "designId": design["designId"],
                "description": design["description"],
                "occurrenceCount": design["occurrenceCount"],
                "pageCount": design["pageCount"],
            }
            for design in sorted(
                profile["designCatalog"].values(),
                key=lambda item: (-item.get("pageCount", 0), item["designId"]),
            )
            if design.get("occurrenceCount", 0) > 0
        ],
        "measurementCaveat": "measured sizes come from pixel projection and are "
                             "approximate; trust the image and correct them",
        "geometryNotes": geometry.get("notes") or [],
    }
    write_json(os.path.join(args.work_dir, "brief.json"), brief)
    emit({
        "ok": True,
        "page": brief["page"],
        "blockCount": len(kept),
        "minorBlocksOmitted": minor,
        "blocksTruncated": truncated,
        "columnCount": brief["columnCount"],
        "panels": len(brief["panels"]),
        "blank": brief["blank"],
        "output": os.path.join(args.work_dir, "brief.json"),
    })


if __name__ == "__main__":
    main()
