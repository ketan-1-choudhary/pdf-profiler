---
name: book-style-profiler
description: "Profile every page of a book PDF sequentially to identify recurring visual components (chapter/unit headings, notices, fun-fact boxes, and similar), record page assignments and bounding boxes, and maintain a local JSON catalog with occurrence counts. Use before PDF-to-HTML conversion; this skill discovers reusable item IDs and descriptions only, does not convert pages, and does not use S3 or DynamoDB."
---

# Book Style Profiler (Local POC)

Discover semantically reusable book components page by page. The vision model decides what an item is, what to call it, and whether it matches a known design. Scripts validate those decisions and maintain counts. This profiler does **not** create HTML/CSS, call S3, or write to DynamoDB.

## What is a reusable design?

Examples include chapter headings, unit headings, section headings, notices, fun-fact boxes, exercise headings, captions, and recurring page furniture. Do not register ordinary body paragraphs or individual text strings.

A design ID names the item's purpose and construction, not its visual measurements. Use concise dot-separated lowercase names, for example:

- `heading.chapter-number-inline`
- `heading.unit-number-circle`
- `box.notice`
- `box.fun-fact`

Distinguish components whose construction differs. For example, a number inside a circle to the left of a title is different from a number inline with the title. **Ignore color when deciding identity**: a blue and an orange notice with the same purpose and construction share an ID. Descriptions state purpose and construction in one sentence; do not encode color, exact font, or exact size.

## Local profile format

The only persistent output is one local `profile.json`:

```json
{
  "schemaVersion": 1,
  "pdfId": "book-id",
  "pageCountTotal": 300,
  "minPageFrequency": 2,
  "pagesProfiled": 2,
  "missingPages": [3, 4],
  "designCatalog": {
    "heading.unit-number-circle": {
      "designId": "heading.unit-number-circle",
      "description": "Unit number sits in a circle to the left of the unit title.",
      "firstSeenPage": 1,
      "occurrenceCount": 1,
      "pageCount": 1,
      "pages": [1],
      "reusable": false
    }
  },
  "pageDesigns": {
    "1": {
      "page": 1,
      "designs": [
        { "designId": "heading.unit-number-circle", "bbox": { "x": 80, "y": 110, "width": 720, "height": 90 }, "confidence": 0.94 }
      ],
      "uncertain": []
    }
  }
}
```

`pageDesigns` is the source of truth for counts. Every visible instance is an entry, so `occurrenceCount` counts instances and `pageCount` counts distinct pages. A design becomes `reusable` after it appears on `minPageFrequency` distinct pages (default 2). Single-page candidates remain in the local catalog for inspection and can be matched on later pages. Reprocessing a page replaces that page's assignment and recomputes counts, so it does not double-count.

## Inputs and storage

For each page, the caller supplies the PDF path, 1-based page number, a scratch directory `$WORK`, a local profile path `$PROFILE_JSON`, and a stable PDF ID. `$PROFILE_JSON` must be the same file for every page of this PDF. Process pages in ascending order; do not run pages in parallel.

The skill root is the directory containing `SKILL.md` and `scripts/`. Run commands from that directory. Dependencies:

- Poppler tools: `pdftoppm`, `pdfinfo`, and optionally `pdftotext`.
- `numpy`, `Pillow`.
- `pdfplumber` is optional and adds exact font measurements when the PDF has a text layer.

## Per-page workflow

For each page, complete all steps before moving to the next page:

1. Render the page with a fixed 1540px long edge. The aspect ratio is preserved,
   so width and height may vary when the PDF page shapes vary. Bounding boxes
   are integer pixels in this exact rendered image; consumers must use the same
   render scale to use them directly.

   ```bash
   python3 scripts/render_norm.py --pdf <pdf-path> --page <N> --work-dir "$WORK"
   ```

2. Measure pixel geometry and optional text-layer font metrics:

   ```bash
   python3 scripts/extract_geometry.py --work-dir "$WORK" --pdf <pdf-path> --page <N>
   ```

3. Build a bounded brief, including the currently known IDs and one-line descriptions:

   ```bash
   python3 scripts/prepare_brief.py --work-dir "$WORK" --profile-json "$PROFILE_JSON"
   ```

4. Inspect `$WORK/page.png` and `$WORK/brief.json`. The image is ground truth; geometry estimates are hints. Compare each possible component against `knownDesigns` in the brief. Reuse a known ID when purpose and construction match, even if color differs. Create a new ID only for a distinct reusable pattern. Skip plain body paragraphs. Save only new definitions and the instances visible on this page as `$WORK/observation_raw.json`.

   Required model output:

   ```json
   {
     "page": 1,
     "newDesigns": [
       { "designId": "heading.unit-number-circle", "description": "Unit number sits in a circle to the left of the unit title." }
     ],
     "designs": [
       { "designId": "heading.unit-number-circle", "bbox": { "x": 80, "y": 110, "width": 720, "height": 90 }, "confidence": 0.94 }
     ],
     "uncertain": []
   }
   ```

  `newDesigns` contains only IDs absent from `knownDesigns`. Each occurrence references either one of those new IDs or an existing known ID. Report a separate occurrence for each visible instance, including repeated instances on the same page. `bbox` is integer pixels in the supplied image, whose long edge is 1540px. Put ambiguous possible components in `uncertain` with `label`, `reason`, and an in-bounds `bbox`; do not force an uncertain match.

5. Validate IDs, definitions, references, confidence values, and bounding boxes:

   ```bash
   python3 scripts/validate_observation.py --work-dir "$WORK" --profile-json "$PROFILE_JSON"
   ```

   If validation fails, fix `observation_raw.json` and retry once. Do not silently coerce a malformed ID or bbox.

6. Merge this page into the one local profile file:

   ```bash
   python3 scripts/merge_observation.py \
     --work-dir "$WORK" --profile-json "$PROFILE_JSON" \
     --pdf-id "<pdf-id>" --page-count-total <total-pages>
   ```

   The merge prints new and matched IDs, current reusable-design count, and pages still missing. The profile is written atomically. Keep `$PROFILE_JSON`; `$WORK` is disposable.

7. Continue with the next page number, using the updated profile to build its brief. After the final page, inspect `designCatalog` for repeated designs and `pageDesigns` for each page's assigned IDs and bounding boxes.

8. Export the split local JSON files. New profiles already store page dimensions.
   For older profiles without dimensions, provide the source PDF so the exporter
   can recover them and validate pixel bounds:

   ```bash
   python3 scripts/export_profile.py \
     --profile-json "$PROFILE_JSON" \
     --out-dir <output-dir> \
     --pdf <pdf-path>
   ```

   The exporter writes `catalog.json` and `pages/<N>/designs.json`. Page files
   contain only the `designs` key and retain integer pixel bboxes. It prints
   placeholder S3 keys but does not upload anything.

## Decision rules

- Match by **purpose and construction**, not by color or exact measurements.
- A different arrangement of parts is a different design ID.
- When a component's purpose or construction is genuinely ambiguous, use `uncertain`; do not invent a match.
- Use IDs from `knownDesigns` exactly. Do not create synonyms for an existing design.
- New IDs must match `category.specific-name` using lowercase letters, digits, and hyphens. Categories are model-chosen (such as `heading`, `box`, `caption`, or `furniture`); there is no closed component vocabulary.
- The description is a concise semantic/construction label, not a full style specification.
- The profiler records no prose content and makes no HTML/CSS decisions.
