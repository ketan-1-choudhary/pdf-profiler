#!/usr/bin/env python3
"""Merge one validated page into a local profile.json."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_lib import emit, fail, load_profile, merge_page, read_json, write_json


def main():
    parser = argparse.ArgumentParser(description="Merge one page observation into a local design profile")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--profile-json", required=True, help="local profile.json shared by sequential page runs")
    parser.add_argument("--observation", default=None)
    parser.add_argument("--pdf-id", default=None)
    parser.add_argument("--page-count-total", type=int, default=None)
    parser.add_argument("--min-page-frequency", type=int, default=None,
                        help="distinct pages required to mark a design reusable")
    args = parser.parse_args()

    if args.min_page_frequency is not None and args.min_page_frequency < 1:
        fail("--min-page-frequency must be at least 1")
    observation_path = args.observation or os.path.join(args.work_dir, "observation.json")
    observation = read_json(observation_path)
    if not isinstance(observation, dict):
        fail("validated observation not found; run validate_observation.py first", path=observation_path)

    try:
        profile = load_profile(args.profile_json, args.pdf_id, args.page_count_total,
                               args.min_page_frequency)
        before = set(profile["designCatalog"])
        page = observation.get("page")
        existing_page = str(page) in profile["pageDesigns"]
        profile = merge_page(profile, observation)
        write_json(args.profile_json, profile)
    except (TypeError, ValueError) as error:
        fail(str(error), code=3)

    page_record = profile["pageDesigns"][str(page)]
    page_ids = {item["designId"] for item in page_record["designs"]}
    new_ids = page_ids - before
    emit({
        "ok": True,
        "page": page,
        "replacedExistingPage": existing_page,
        "occurrenceCount": len(page_record["designs"]),
        "newDesignIds": sorted(new_ids),
        "matchedDesignIds": sorted(page_ids - new_ids),
        "designCount": len(profile["designCatalog"]),
        "reusableDesignCount": sum(1 for d in profile["designCatalog"].values() if d["reusable"]),
        "pagesProfiled": profile["pagesProfiled"],
        "missingPages": profile["missingPages"],
        "profileJson": os.path.abspath(args.profile_json),
    })


if __name__ == "__main__":
    main()
