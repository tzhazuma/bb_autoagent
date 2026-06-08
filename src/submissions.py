"""Fetch student submissions from Blackboard Grade Center via Playwright."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.auth import BlackboardAuth

logger = logging.getLogger(__name__)

SEL_GRADEBOOK_LINK = "a[href*='gradebook']"
SEL_FULL_GRADE_CENTER = "a[href*='enterGradeCenter']"
SEL_SUBMISSIONS_LINK = "a[href*='listAllSubmissions']"
SEL_DOWNLOAD_LINK = "a[href*='download'], .downloadLink"
SEL_ATTEMPT_LINK = "a[href*='attempt'], a[href*='inline']"
SEL_GRADE_CELL = "td.cell, td[id*='grade']"
SEL_SUBMISSION_CONTENT = "#content_text, .vtbegenerated, .submission-content"

_TEXT_EXTENSIONS = {".txt", ".py", ".java", ".cpp", ".c", ".h", ".hpp", ".md", ".json", ".xml", ".csv", ".yaml", ".yml"}

MAX_RETRIES = 3
RETRY_DELAY = 2.0
NAV_TIMEOUT = 30000
DOWNLOAD_TIMEOUT = 60000


class Submission(BaseModel):
    student_name: str
    student_id: str
    file_path: Path | None = None
    submission_time: datetime | None = None
    content_text: str = ""
    file_type: str = ""
    download_time: datetime | None = None
    metadata: dict = Field(default_factory=dict)


class SubmissionFetcher:
    """Navigates Blackboard Grade Center and downloads student submissions."""

    def __init__(self, auth: BlackboardAuth, config: dict) -> None:
        self.auth = auth
        self.base_url = auth.base_url
        self.download_dir = Path(config.get("download_dir", "./downloads"))
        self.supported_formats: list[str] = config.get(
            "supported_formats",
            [".pdf", ".docx", ".doc", ".txt", ".py", ".java", ".cpp", ".c", ".ipynb"],
        )
        self._page: Page | None = None

    async def _get_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = await self.auth.get_page()
        return self._page

    async def _retry(self, coro_fn, *, retries: int = MAX_RETRIES, label: str = ""):
        """Retry an async callable up to `retries` times with exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await coro_fn()
            except (PlaywrightTimeout, OSError) as exc:
                last_exc = exc
                delay = RETRY_DELAY * attempt
                logger.warning(
                    "[%s] attempt %d/%d failed: %s — retrying in %.1fs",
                    label or coro_fn.__name__, attempt, retries, exc, delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(
            f"{label or coro_fn.__name__} failed after {retries} attempts"
        ) from last_exc

    # ── Navigation ─────────────────────────────────────────────────────

    async def _navigate_to_grade_center(self, course_id: str) -> None:
        page = await self._get_page()
        base = self.base_url

        course_url = f"{base}/webapps/blackboard/execute/launcher?type=Course&id={course_id}"
        logger.info("Opening course page: %s", course_url)
        await page.goto(course_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        gradebook = await page.query_selector(SEL_GRADEBOOK_LINK)
        if gradebook is None:
            ctl_panel = await page.query_selector(
                "a[href*='controlPanel'], a[href*='courseMenu']"
            )
            if ctl_panel:
                await ctl_panel.click()
                await asyncio.sleep(1)
                gradebook = await page.query_selector(SEL_GRADEBOOK_LINK)

        if gradebook is None:
            raise RuntimeError(
                f"Could not find Grade Center link on course page for {course_id}"
            )
        await gradebook.click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)

        full_gc = await page.query_selector(SEL_FULL_GRADE_CENTER)
        if full_gc:
            await full_gc.click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)
            logger.info("Navigated to Full Grade Center")
        else:
            logger.warning("Full Grade Center link not found; may already be on it")

    async def _get_submission_list(self) -> list[dict]:
        """Parse visible student rows from the Grade Center table."""
        page = await self._get_page()

        sub_link = await page.query_selector(SEL_SUBMISSIONS_LINK)
        if sub_link:
            await sub_link.click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)

        try:
            await page.wait_for_selector(
                "#gradebook-grid, #listContainerDiv, table.egTable, #gradebook_wrap",
                timeout=NAV_TIMEOUT,
            )
        except PlaywrightTimeout:
            logger.warning("Grade center grid not detected — attempting fallback parse")

        students: list[dict] = []

        rows = await page.query_selector_all(
            "table.egTable tr, #gradebook-grid tr, #listContainerDiv tr"
        )

        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 2:
                continue

            name_text = ""
            for cell in cells[:3]:
                txt = (await cell.text_content() or "").strip()
                if txt and not txt.startswith("Last"):
                    name_text = txt
                    break

            if not name_text:
                continue

            row_id = await row.get_attribute("id") or ""
            sid = ""
            if "userid:" in row_id.lower() or "user_id:" in row_id.lower():
                sid = row_id.split(":")[-1].strip("_")
            else:
                for cell in cells:
                    txt = (await cell.text_content() or "").strip()
                    if txt.isdigit() and len(txt) >= 6:
                        sid = txt
                        break

            students.append(
                {"student_name": name_text, "student_id": sid, "row": row}
            )

        logger.info("Found %d student rows in Grade Center", len(students))
        return students

    async def _download_submission(self, student_info: dict) -> list[Path]:
        page = await self._get_page()
        row = student_info["row"]
        sid = student_info["student_id"] or "unknown"
        name = student_info["student_name"]

        dest_dir = self.download_dir / sid
        dest_dir.mkdir(parents=True, exist_ok=True)

        downloaded: list[Path] = []

        attempt_link = await row.query_selector(SEL_ATTEMPT_LINK)
        if attempt_link is None:
            grade_cell = await row.query_selector(SEL_GRADE_CELL)
            if grade_cell:
                await grade_cell.click()
                await asyncio.sleep(1)
                attempt_link = await page.query_selector(SEL_ATTEMPT_LINK)

        if attempt_link is None:
            logger.warning("No submission attempt found for %s (%s)", name, sid)
            return downloaded

        href = await attempt_link.get_attribute("href") or ""
        if href.startswith("http"):
            attempt_url = href
        elif href.startswith("/"):
            attempt_url = f"{self.base_url}{href}"
        else:
            attempt_url = page.url.rsplit("/", 1)[0] + f"/{href}"

        await page.goto(attempt_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        download_links = await page.query_selector_all(SEL_DOWNLOAD_LINK)
        if not download_links:
            inline_text = await self._extract_inline_content(page)
            if inline_text:
                text_path = dest_dir / "submission.txt"
                text_path.write_text(inline_text, encoding="utf-8")
                downloaded.append(text_path)
                logger.info("Saved inline submission for %s → %s", name, text_path)
            else:
                logger.warning(
                    "No downloadable files or inline content for %s (%s)", name, sid
                )
            return downloaded

        for link in download_links:
            try:
                file_path = await self._download_file(link, dest_dir, page)
                if file_path:
                    downloaded.append(file_path)
                    logger.info("Downloaded %s → %s", name, file_path)
            except Exception as exc:
                logger.error("Failed to download file for %s: %s", name, exc)

        return downloaded

    async def _download_file(
        self, link_element, dest_dir: Path, page: Page
    ) -> Path | None:
        link_text = (await link_element.text_content() or "file").strip()
        href = await link_element.get_attribute("href") or ""

        try:
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as dl_info:
                await link_element.click()
            download = dl_info.value
            suggested = download.suggested_filename or link_text
            dest = dest_dir / self._sanitize_filename(suggested)
            await download.save_as(str(dest))
            return dest
        except PlaywrightTimeout:
            logger.warning("Download event timeout for '%s'; checking for new tab", link_text)
            if len(page.context.pages) > 1:
                new_page = page.context.pages[-1]
                content = await new_page.content()
                ext = Path(href).suffix or ".html"
                dest = dest_dir / self._sanitize_filename(link_text + ext)
                dest.write_text(content, encoding="utf-8")
                await new_page.close()
                return dest
            return None

    async def _extract_inline_content(self, page: Page) -> str:
        content_el = await page.query_selector(SEL_SUBMISSION_CONTENT)
        if content_el:
            return (await content_el.text_content() or "").strip()

        frames = page.frames
        for frame in frames:
            if frame == page.main_frame:
                continue
            body = await frame.query_selector("body")
            if body:
                text = (await body.text_content() or "").strip()
                if text:
                    return text

        return ""

    async def _extract_text(self, file_path: Path) -> str:
        if not file_path.exists():
            return ""

        ext = file_path.suffix.lower()

        if ext in _TEXT_EXTENSIONS:
            try:
                return file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Could not read %s: %s", file_path, exc)
                return ""

        if ext == ".ipynb":
            try:
                nb = json.loads(file_path.read_text(encoding="utf-8"))
                cells = nb.get("cells", [])
                parts: list[str] = []
                for cell in cells:
                    if cell.get("cell_type") in ("code", "markdown"):
                        parts.append("".join(cell.get("source", [])))
                return "\n\n".join(parts)
            except Exception as exc:
                logger.warning("Failed to parse notebook %s: %s", file_path, exc)
                return ""

        if ext in (".pdf",):
            return f"[Binary file: {file_path.name} — PDF, requires external parser]"

        if ext in (".docx", ".doc"):
            return f"[Binary file: {file_path.name} — Word document, requires external parser]"

        mime, _ = mimetypes.guess_type(str(file_path))
        return f"[Binary file: {file_path.name} — type: {mime or 'unknown'}]"

    async def fetch_all(
        self,
        course_id: str,
        assignment_id: str,
        download_dir: str = "./downloads",
    ) -> list[Submission]:
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Fetching submissions for course=%s assignment=%s", course_id, assignment_id,
        )

        await self._retry(
            lambda: self._navigate_to_grade_center(course_id),
            label="navigate_grade_center",
        )

        student_rows = await self._get_submission_list()
        if not student_rows:
            logger.warning("No students found in Grade Center")
            return []

        submissions: list[Submission] = []

        for idx, student in enumerate(student_rows, 1):
            name = student["student_name"]
            sid = student["student_id"]
            logger.info(
                "[%d/%d] Processing %s (%s)", idx, len(student_rows), name, sid or "no-id",
            )

            try:
                files = await self._download_submission(student)
            except Exception as exc:
                logger.error("Error fetching submission for %s: %s", name, exc)
                submissions.append(
                    Submission(
                        student_name=name,
                        student_id=sid,
                        content_text=f"[Error: {exc}]",
                        metadata={"error": str(exc)},
                    )
                )
                continue

            if not files:
                submissions.append(
                    Submission(
                        student_name=name,
                        student_id=sid,
                        content_text="",
                        metadata={"status": "no_submission"},
                    )
                )
                continue

            combined_text_parts: list[str] = []
            primary_file = files[0]
            for f in files:
                text = await self._extract_text(f)
                if text:
                    combined_text_parts.append(f"--- {f.name} ---\n{text}")

            submissions.append(
                Submission(
                    student_name=name,
                    student_id=sid,
                    file_path=primary_file,
                    content_text="\n\n".join(combined_text_parts),
                    file_type=primary_file.suffix.lower(),
                    download_time=datetime.now(),
                    metadata={
                        "all_files": [str(f) for f in files],
                        "file_count": len(files),
                    },
                )
            )

            await asyncio.sleep(0.5)

        logger.info("Fetched %d submissions total", len(submissions))
        return submissions

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
