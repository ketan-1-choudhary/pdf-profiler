#!/usr/bin/env python3
"""Shared local JSON schema and deterministic profile bookkeeping."""

import json
import os
import re
import tempfile

SCHEMA_VERSION = 1
TARGET_LONG_EDGE = 1540
DESIGN_ID_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    """Atomically replace a JSON file so an interrupted run cannot truncate it."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def fail(message, code=2, **extra):
    import sys
    print(json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


def parse_hex(value):
    if not isinstance(value, str):
        return None
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    if len(digits) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", digits):
        return None
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))


def to_hex(rgb):
    values = (max(0, min(255, int(round(value)))) for value in rgb)
    return "#" + "".join(f"{value:02x}" for value in values)


def srgb_to_lab(rgb):
    def linear(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(float(value)) for value in rgb)
    x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / 0.95047
    y = red * 0.2126 + green * 0.7152 + blue * 0.0722
    z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / 1.08883

    def f(value):
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e76(lab_a, lab_b):
    return sum((a - b) ** 2 for a, b in zip(lab_a, lab_b)) ** 0.5


def colours_are_close(hex_a, hex_b, tolerance=6.0):
    rgb_a, rgb_b = parse_hex(hex_a), parse_hex(hex_b)
    if rgb_a is None or rgb_b is None:
        return hex_a == hex_b
    return delta_e76(srgb_to_lab(rgb_a), srgb_to_lab(rgb_b)) <= tolerance


def valid_design_id(value):
    return isinstance(value, str) and DESIGN_ID_RE.fullmatch(value) is not None


def valid_bbox(value, width, height):
    if not isinstance(value, dict):
        return False
    coords = [value.get(k) for k in ("x", "y", "width", "height")]
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in coords):
        return False
    x, y, box_width, box_height = coords
    return (
        all(float(n).is_integer() for n in coords)
        and x >= 0 and y >= 0 and box_width > 0 and box_height > 0
        and x + box_width <= width and y + box_height <= height
    )


def empty_profile(pdf_id=None, page_count_total=None, min_page_frequency=2):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "pdfId": pdf_id,
        "pageCountTotal": page_count_total,
        "minPageFrequency": min_page_frequency,
        "pagesProfiled": 0,
        "missingPages": [],
        "designCatalog": {},
        "pageDesigns": {},
    }


def load_profile(path, pdf_id=None, page_count_total=None, min_page_frequency=None):
    profile = read_json(path)
    if profile is None:
        return empty_profile(pdf_id, page_count_total,
                             min_page_frequency if min_page_frequency is not None else 2)
    if not isinstance(profile, dict) or profile.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported or invalid profile schema in {path}")
    if pdf_id and profile.get("pdfId") not in (None, pdf_id):
        raise ValueError("Profile belongs to a different PDF")
    if page_count_total and profile.get("pageCountTotal") not in (None, page_count_total):
        raise ValueError("Profile page count does not match this PDF")
    profile["pdfId"] = pdf_id or profile.get("pdfId")
    profile["pageCountTotal"] = page_count_total or profile.get("pageCountTotal")
    profile["minPageFrequency"] = (
        min_page_frequency if min_page_frequency is not None
        else profile.get("minPageFrequency", 2)
    )
    if not isinstance(profile.get("designCatalog"), dict) or not isinstance(profile.get("pageDesigns"), dict):
        raise ValueError(f"Invalid catalog or page map in {path}")
    profile.setdefault("designCatalog", {})
    profile.setdefault("pageDesigns", {})
    return profile


def _recompute(profile):
    totals = {}
    for page_key, page_record in profile["pageDesigns"].items():
        page_number = int(page_key)
        for occurrence in page_record.get("designs", []):
            design_id = occurrence["designId"]
            slot = totals.setdefault(design_id, {"occurrenceCount": 0, "pages": set()})
            slot["occurrenceCount"] += 1
            slot["pages"].add(page_number)

    threshold = max(1, int(profile.get("minPageFrequency", 2)))
    for design_id in list(profile["designCatalog"]):
        if design_id not in totals:
            del profile["designCatalog"][design_id]
    for design_id, definition in profile["designCatalog"].items():
        counts = totals.get(design_id, {"occurrenceCount": 0, "pages": set()})
        pages = sorted(counts["pages"])
        definition["occurrenceCount"] = counts["occurrenceCount"]
        definition["pageCount"] = len(pages)
        definition["pages"] = pages
        definition["firstSeenPage"] = pages[0] if pages else definition.get("firstSeenPage")
        definition["reusable"] = len(pages) >= threshold

    profiled_pages = sorted(int(page) for page in profile["pageDesigns"])
    profile["pagesProfiled"] = len(profiled_pages)
    total_pages = profile.get("pageCountTotal")
    if isinstance(total_pages, int) and total_pages > 0:
        seen = set(profiled_pages)
        profile["missingPages"] = [p for p in range(1, total_pages + 1) if p not in seen]
    else:
        profile["missingPages"] = []
    return profile


def merge_page(profile, observation):
    """Upsert one page and recompute catalog counts; safe to rerun that page."""
    page = observation.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("Observation page must be a positive integer")
    total = profile.get("pageCountTotal")
    if isinstance(total, int) and page > total:
        raise ValueError(f"Page {page} exceeds document page count {total}")

    definitions = observation.get("newDesigns", [])
    occurrences = observation.get("designs", [])
    if not isinstance(definitions, list) or not isinstance(occurrences, list):
        raise ValueError("newDesigns and designs must be arrays")

    page_definitions = {}
    for item in definitions:
        if not isinstance(item, dict):
            raise ValueError("Each new design must be an object")
        design_id = item.get("designId")
        description = item.get("description")
        if not valid_design_id(design_id):
            raise ValueError(f"Invalid designId: {design_id!r}")
        if not isinstance(description, str) or not description.strip() or len(description) > 240:
            raise ValueError(f"Design {design_id} needs a 1-240 character description")
        page_definitions[design_id] = description.strip()

    known_ids = set(profile["designCatalog"]) | set(page_definitions)
    normalized_occurrences = []
    width = int(observation.get("imageWidth") or 0)
    height = int(observation.get("imageHeight") or 0)
    for item in occurrences:
        if not isinstance(item, dict):
            raise ValueError("Each page design occurrence must be an object")
        design_id = item.get("designId")
        if design_id not in known_ids:
            raise ValueError(f"Occurrence references unknown designId: {design_id!r}")
        bbox = item.get("bbox")
        if not valid_bbox(bbox, width, height):
            raise ValueError(f"Design {design_id} has an invalid or out-of-bounds bbox")
        confidence = item.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"Design {design_id} confidence must be between 0 and 1")
        normalized_occurrences.append({
            "designId": design_id,
            "bbox": {k: int(bbox[k]) for k in ("x", "y", "width", "height")},
            "confidence": round(float(confidence), 3),
        })

    for design_id, description in page_definitions.items():
        if design_id not in profile["designCatalog"]:
            profile["designCatalog"][design_id] = {
                "designId": design_id,
                "description": description,
                "firstSeenPage": page,
                "occurrenceCount": 0,
                "pageCount": 0,
                "pages": [],
                "reusable": False,
            }

    profile["pageDesigns"][str(page)] = {
        "page": page,
        "imageWidth": int(observation.get("imageWidth") or 0),
        "imageHeight": int(observation.get("imageHeight") or 0),
        "designs": normalized_occurrences,
        "uncertain": observation.get("uncertain", []),
    }
    return _recompute(profile)
