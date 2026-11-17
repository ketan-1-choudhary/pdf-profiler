#!/usr/bin/env python3
"""Export the working profile to a catalog and per-page pixel-coordinate JSON files."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import emit, fail, read_json, valid_bbox, write_json  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_S3_BASE_KEY = "<S3_BUCKET>/<PDF_ID>"


def page_dimensions_from_pdf(pdf_path, page):
    with tempfile.TemporaryDirectory(prefix="book-profile-render-") as work_dir:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "render_norm.py"),
             "--pdf", str(pdf_path), "--page", str(page), "--work-dir", work_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Could not render page {page} to recover its dimensions: "
                             f"{result.stderr.strip() or result.stdout.strip()}")
        meta = read_json(os.path.join(work_dir, "page_meta.json"))
        if not isinstance(meta, dict):
            raise ValueError(f"Missing page metadata while rendering page {page}")
        width, height = meta.get("imageWidth"), meta.get("imageHeight")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise ValueError(f"Invalid rendered dimensions for page {page}")
        return width, height


def validate_pixel_bbox(bbox, image_width, image_height, design_id, page):
    if not valid_bbox(bbox, image_width, image_height):
        raise ValueError(f"Page {page} design {design_id} has an invalid pixel bbox")
    return {key: int(bbox[key]) for key in ("x", "y", "width", "height")}


def main():
    parser = argparse.ArgumentParser(description="Split a local profile into catalog and pixel-coordinate page files")
    parser.add_argument("--profile-json", required=True, help="working combined profile.json")
    parser.add_argument("--out-dir", required=True, help="directory for catalog.json and pages/")
    parser.add_argument("--pdf", default=None,
                        help="source PDF; required only when page dimensions were not saved in the profile")
    parser.add_argument("--s3-base-key", default=DEFAULT_S3_BASE_KEY,
                        help="placeholder/object prefix for future upload; no upload is performed")
    args = parser.parse_args()

    profile = read_json(args.profile_json)
    if not isinstance(profile, dict) or profile.get("schemaVersion") != 1:
        fail("working profile is missing or has an unsupported schema", profileJson=args.profile_json)
    catalog = profile.get("designCatalog")
    page_map = profile.get("pageDesigns")
    if not isinstance(catalog, dict) or not isinstance(page_map, dict):
        fail("working profile must contain designCatalog and pageDesigns")

    try:
        catalog_designs = {}
        for design_id, design in sorted(catalog.items()):
            description = design.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"Catalog design {design_id!r} has no description")
            catalog_designs[design_id] = {
                "designId": design_id,
                "description": description,
                "occurrenceCount": int(design.get("occurrenceCount", 0)),
                "reusable": bool(design.get("reusable", False)),
                "html": "",
                "css": "",
            }

        rendered_dimensions = {}
        page_outputs = []
        for page_key, page_record in sorted(page_map.items(), key=lambda pair: int(pair[0])):
            page = int(page_key)
            if page_record.get("page", page) != page:
                raise ValueError(f"Page key {page_key} disagrees with its page record")
            width, height = page_record.get("imageWidth"), page_record.get("imageHeight")
            if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
                if not args.pdf:
                    raise ValueError(
                        f"Page {page} has no saved image dimensions; provide --pdf to render them")
                if page not in rendered_dimensions:
                    rendered_dimensions[page] = page_dimensions_from_pdf(args.pdf, page)
                width, height = rendered_dimensions[page]

            exported_designs = []
            for occurrence in page_record.get("designs", []):
                design_id = occurrence.get("designId")
                if design_id not in catalog_designs:
                    raise ValueError(f"Page {page} references missing catalog design {design_id!r}")
                exported_designs.append({
                    "designId": design_id,
                    "bbox": validate_pixel_bbox(occurrence.get("bbox"), width, height, design_id, page),
                })
            page_outputs.append((page, {"designs": exported_designs}))

        base_key = args.s3_base_key.rstrip("/")
        output_root = Path(args.out_dir).resolve()
        catalog_path = output_root / "catalog.json"
        catalog_payload = {
            "schemaVersion": 1,
            "pdfId": profile.get("pdfId"),
            "designs": catalog_designs,
        }
        write_json(str(catalog_path), catalog_payload)

        page_manifest = []
        for page, payload in page_outputs:
            page_path = output_root / "pages" / str(page) / "designs.json"
            write_json(str(page_path), payload)
            page_manifest.append({
                "page": page,
                "localPath": str(page_path),
                "s3KeyPlaceholder": f"{base_key}/pages/{page}/designs.json",
            })

    except (OSError, TypeError, ValueError) as error:
        fail(str(error), code=3)

    emit({
        "ok": True,
        "catalog": {
            "localPath": str(catalog_path),
            "s3KeyPlaceholder": f"{base_key}/designs/catalog.json",
            "designCount": len(catalog_designs),
        },
        "pages": page_manifest,
        "note": "Local export only; S3 upload is not implemented.",
    })


if __name__ == "__main__":
    main()
