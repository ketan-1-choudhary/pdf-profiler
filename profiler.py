import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query


class Profiler:
    def __init__(self, s3, project_root=None):
        self.s3 = s3
        self.project_root = Path(project_root or os.environ.get("PROJECT_ROOT", "/app"))
        self.skill_root = self.project_root / ".claude" / "skills" / "book-style-profiler"

    def _save(self, bucket, key, payload):
        self.s3.put_object(Bucket=bucket, Key=key,
                           Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           ContentType="application/json")

    async def _run_skill(self, pdf_path, pdf_id, total_pages, work, profile_path):
        if not (self.skill_root / "SKILL.md").is_file():
            raise FileNotFoundError(f"Profiler skill not found at {self.skill_root}")
        prompt = (
            f"Here is a book PDF at {pdf_path}. Use the `book-style-profiler` skill to profile it.\n"
            f"- PDF ID: {pdf_id}\n"
            f"- Total pages: {total_pages}\n"
            f"- Work directory: {work}\n"
            f"- Profile JSON: {profile_path}\n"
            f"- Skill root: {self.skill_root}"
        )
        options = ClaudeAgentOptions(
            cwd=str(self.project_root), setting_sources=["project"],
            skills=["book-style-profiler"],
            allowed_tools=["Read", "Write", "Bash"], permission_mode="bypassPermissions",
            max_buffer_size=int(os.environ.get("SDK_MAX_BUFFER_BYTES", 20 * 1024 * 1024)),
            env={**os.environ, "CLAUDE_CODE_USE_BEDROCK": "1",
                 "ANTHROPIC_MODEL": os.environ.get(
                     "ANTHROPIC_BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
                 "PROJECT_ROOT": str(self.project_root),
                 "SKILL_ROOT": str(self.skill_root),
                 "WORK": str(work), "PROFILE_JSON": str(profile_path),
                 "PDF_PATH": str(pdf_path),
                 "PDF_ID": pdf_id, "PAGE_COUNT_TOTAL": str(total_pages)},
        )
        succeeded = False
        async for message in query(prompt=prompt, options=options):
            if getattr(message, "error", None) or getattr(message, "is_error", False):
                raise RuntimeError(f"Profiler agent failed: {message}")
            if type(message).__name__ == "ResultMessage":
                succeeded = getattr(message, "subtype", None) == "success"
        if not succeeded:
            raise RuntimeError(f"Profiler skill did not finish successfully for {pdf_id}")

    async def profile(self, pdf_path, bucket, pdf_id, total_pages, work_dir):
        root = Path(work_dir)
        root.mkdir(parents=True, exist_ok=True)
        profile_path = root / "profile.json"
        work = root / "work"
        work.mkdir(exist_ok=True)
        threshold = int(os.environ.get("MIN_DESIGN_PAGE_FREQUENCY", "2"))
        if threshold < 1:
            raise ValueError("MIN_DESIGN_PAGE_FREQUENCY must be positive")

        await self._run_skill(pdf_path, pdf_id, total_pages, work, profile_path)

        state = json.loads(profile_path.read_text(encoding="utf-8"))
        if state.get("pdfId") != pdf_id or state.get("pageCountTotal") != total_pages:
            raise ValueError("Profiler skill wrote a profile for a different PDF")
        page_designs = state.get("pageDesigns")
        if not isinstance(page_designs, dict):
            raise ValueError("Profiler skill wrote a profile without page designs")
        missing = [page for page in range(1, total_pages + 1) if str(page) not in page_designs]
        if missing:
            raise RuntimeError(f"Profiler skill left pages unprofiled: {missing}")

        reusable = {design_id for design_id, design in state["designCatalog"].items()
                    if design["pageCount"] >= threshold}
        for page in range(1, total_pages + 1):
            self._save(bucket, f"{pdf_id}/pages/{page}/designs.json", {
                "designs": [{"designId": item["designId"], "bbox": item["bbox"]}
                            for item in page_designs[str(page)]["designs"]
                            if item["designId"] in reusable],
            })
        final = {
            "schemaVersion": 1, "pdfId": pdf_id, "pageCountTotal": total_pages,
            "phase": "ready", "enqueuedPages": [],
            "designs": {design_id: {
                "designId": design_id, "description": design["description"],
                "occurrenceCount": design["occurrenceCount"], "html": "", "css": "",
            } for design_id, design in state["designCatalog"].items() if design_id in reusable},
        }
        self._save(bucket, f"{pdf_id}/designs/catalog.json", final)
        logging.info("Profiled %s pages for %s; %s reusable designs",
                     total_pages, pdf_id, len(final["designs"]))
        return final
