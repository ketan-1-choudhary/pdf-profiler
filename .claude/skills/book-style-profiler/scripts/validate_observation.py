#!/usr/bin/env python3
"""Validate model-named designs and page-local bounding boxes."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import emit, fail, load_profile, read_json, valid_bbox, valid_design_id, write_json


def main():
    parser = argparse.ArgumentParser(description="Validate one page's design assignments")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--raw", default=None)
    parser.add_argument("--profile-json", default=None,
                        help="existing local profile; used to validate known design IDs")
    args = parser.parse_args()

    raw_path = args.raw or os.path.join(args.work_dir, "observation_raw.json")
    raw = read_json(raw_path)
    brief = read_json(os.path.join(args.work_dir, "brief.json"))
    meta = read_json(os.path.join(args.work_dir, "page_meta.json"))
    geometry = read_json(os.path.join(args.work_dir, "geometry.json"), {}) or {}
    if not isinstance(raw, dict):
        fail("model observation missing or invalid JSON", code=3, path=raw_path)
    if not isinstance(brief, dict) or not isinstance(meta, dict):
        fail("brief.json or page_meta.json missing; run prepare_brief.py first", code=3)

    page = raw.get("page", raw.get("pageNumber"))
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        fail("observation page must be a positive integer", code=3)
    if page != meta.get("page"):
        fail("observation page does not match rendered page", code=3,
             observed=page, rendered=meta.get("page"))

    width = int(meta.get("imageWidth") or 0)
    height = int(meta.get("imageHeight") or 0)
    if width <= 0 or height <= 0:
        fail("render dimensions missing from page_meta.json", code=3)

    profile = load_profile(args.profile_json) if args.profile_json else None
    known = set((profile or {}).get("designCatalog", {}))
    known.update(item.get("designId") for item in (brief.get("knownDesigns") or [])
                 if isinstance(item, dict) and isinstance(item.get("designId"), str))

    errors = []
    definitions = {}
    new_designs = raw.get("newDesigns", [])
    occurrences = raw.get("designs", [])
    if not isinstance(new_designs, list):
        errors.append("newDesigns must be an array")
        new_designs = []
    if not isinstance(occurrences, list):
        errors.append("designs must be an array")
        occurrences = []

    for index, item in enumerate(new_designs):
        where = f"newDesigns[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        design_id = item.get("designId")
        description = item.get("description")
        if not valid_design_id(design_id):
            errors.append(f"{where}.designId must be dot-separated lowercase words (example: heading.unit)")
            continue
        if design_id in known:
            errors.append(f"{where}.designId {design_id!r} already exists; reuse it in designs instead")
            continue
        if not isinstance(description, str) or not description.strip() or len(description.strip()) > 240:
            errors.append(f"{where}.description must be 1-240 characters")
            continue
        if design_id in definitions and definitions[design_id] != description.strip():
            errors.append(f"{where}: conflicting descriptions for {design_id!r}")
            continue
        definitions[design_id] = description.strip()

    geometry_blocks = {block.get("id"): block for block in (geometry.get("blocks") or [])}
    normalized = []
    used_new_ids = set()
    for index, item in enumerate(occurrences):
        where = f"designs[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        design_id = item.get("designId")
        if not valid_design_id(design_id):
            errors.append(f"{where}.designId is invalid")
            continue
        if design_id not in known and design_id not in definitions:
            errors.append(f"{where} references unknown designId {design_id!r}; define it in newDesigns")
            continue

        bbox = item.get("bbox")
        block_ids = item.get("geometryBlockIds") or []
        if not bbox and isinstance(block_ids, list):
            boxes = [geometry_blocks[b].get("bbox") for b in block_ids if b in geometry_blocks]
            boxes = [box for box in boxes if isinstance(box, dict)]
            if boxes:
                x0 = min(box["x"] for box in boxes)
                y0 = min(box["y"] for box in boxes)
                x1 = max(box["x"] + box["width"] for box in boxes)
                y1 = max(box["y"] + box["height"] for box in boxes)
                bbox = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
        if not valid_bbox(bbox, width, height):
            errors.append(f"{where}.bbox must be a positive integer box within the rendered page")
            continue

        confidence = item.get("confidence", 0.8)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{where}.confidence must be between 0 and 1")
            continue
        normalized.append({
            "designId": design_id,
            "bbox": {key: int(bbox[key]) for key in ("x", "y", "width", "height")},
            "confidence": round(float(confidence), 3),
        })
        if design_id in definitions:
            used_new_ids.add(design_id)

    unused = sorted(set(definitions) - used_new_ids)
    if unused:
        errors.append(f"new designs have no page occurrence: {', '.join(unused)}")

    uncertain = raw.get("uncertain", [])
    if not isinstance(uncertain, list):
        errors.append("uncertain must be an array")
        uncertain = []
    normalized_uncertain = []
    for index, item in enumerate(uncertain):
        if not isinstance(item, dict):
            errors.append(f"uncertain[{index}] must be an object")
            continue
        bbox = item.get("bbox")
        label = item.get("label")
        reason = item.get("reason")
        if not isinstance(label, str) or not label.strip() or not isinstance(reason, str) or not reason.strip():
            errors.append(f"uncertain[{index}] needs a label and reason")
            continue
        if not valid_bbox(bbox, width, height):
            errors.append(f"uncertain[{index}].bbox must be within the rendered page")
            continue
        normalized_uncertain.append({
            "label": label.strip()[:120],
            "reason": reason.strip()[:240],
            "bbox": {key: int(bbox[key]) for key in ("x", "y", "width", "height")},
        })

    if errors:
        fail("observation failed validation", code=3, errors=errors[:30],
             hint="correct observation_raw.json and re-run validation")

    observation = {
        "schemaVersion": 1,
        "page": page,
        "pageCountTotal": meta.get("pageCountTotal"),
        "pdfId": profile.get("pdfId") if profile else None,
        "imageWidth": width,
        "imageHeight": height,
        "newDesigns": [
            {"designId": design_id, "description": description}
            for design_id, description in sorted(definitions.items())
        ],
        "designs": normalized,
        "uncertain": normalized_uncertain,
    }
    output = os.path.join(args.work_dir, "observation.json")
    write_json(output, observation)
    emit({
        "ok": True,
        "page": page,
        "newDesignCount": len(definitions),
        "occurrenceCount": len(normalized),
        "uncertainCount": len(normalized_uncertain),
        "designIds": sorted({item["designId"] for item in normalized}),
        "output": output,
    })


if __name__ == "__main__":
    main()
