"""Playwright-based grade uploader for Blackboard Grade Center."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from playwright.async_api import Frame, Page
from pydantic import BaseModel

if TYPE_CHECKING:
    from src.auth import BlackboardAuth

logger = logging.getLogger(__name__)

SEL_GRADE_CENTER_LINK = 'a[href*="gradebook"], a[href*="GradeCenter"], a:has-text("Grade Center"), a:has-text("成绩中心")'
SEL_FULL_GRADE_CENTER = 'a[href*="enterGradeCenter"], a:has-text("Full Grade Center"), a:has-text("完整成绩中心")'
SEL_CONTENT_FRAME = 'iframe#contentFrame, iframe[name="content"], iframe[src*="gradebook"]'
SEL_GRADE_COLUMN_HEADER = 'th.gradeColumnHeader, th a[href*="columnName"]'
SEL_SAVE_BUTTON = 'input[value="Save Changes"], input[value="保存"], button:has-text("Save"), #bottom_Save'
SEL_GRADE_INPUT = 'input[name*="grade"], input.editBoxStyle, input[type="text"]'


class GradeEntry(BaseModel):
    student_name: str
    student_id: str
    score: float
    feedback: Optional[str] = None


class GradebookUploader:
    def __init__(self, auth: BlackboardAuth, config: dict) -> None:
        self.auth = auth
        self.upload_timeout: int = config.get("upload_timeout", 60000)
        self.cell_delay: int = config.get("cell_delay", 500)
        self.verify_after_upload: bool = config.get("verify_after_upload", True)
        self._page: Optional[Page] = None
        self._content_frame: Optional[Frame] = None

    async def _get_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = await self.auth.get_page()
        return self._page

    async def upload_grade(
        self,
        course_id: str,
        column_name: str,
        student_id: str,
        score: float,
        feedback: str = "",
    ) -> bool:
        entry = GradeEntry(
            student_name="",
            student_id=student_id,
            score=score,
            feedback=feedback or None,
        )
        result = await self.upload_batch(course_id, column_name, [entry])
        return result["success"] > 0

    async def upload_batch(
        self,
        course_id: str,
        column_name: str,
        entries: list[GradeEntry],
    ) -> dict:
        result: dict = {"success": 0, "failed": 0, "errors": []}

        if not entries:
            return result

        try:
            await self._navigate_to_grade_center(course_id)
            col_index = await self._find_column(column_name)
            if col_index is None:
                result["errors"].append(f"Column '{column_name}' not found")
                result["failed"] = len(entries)
                return result

            for i, entry in enumerate(entries):
                try:
                    row = await self._find_student_row(entry.student_id)
                    if row is None:
                        result["errors"].append(
                            f"Student '{entry.student_id}' not found"
                        )
                        result["failed"] += 1
                        continue

                    await self._enter_score(row, col_index, entry.score)
                    result["success"] += 1

                    if (i + 1) % 10 == 0:
                        await self._save_changes()

                except Exception as e:
                    result["errors"].append(
                        f"Error for student '{entry.student_id}': {e}"
                    )
                    result["failed"] += 1

            await self._save_changes()

            if self.verify_after_upload and result["success"] > 0:
                for entry in entries:
                    try:
                        verified = await self._verify_upload(
                            entry.student_id, column_name, entry.score
                        )
                        if not verified:
                            logger.warning(
                                f"Verification failed for student '{entry.student_id}'"
                            )
                    except Exception as e:
                        logger.warning(f"Verification error for '{entry.student_id}': {e}")

        except Exception as e:
            logger.error(f"Batch upload failed: {e}")
            result["errors"].append(f"Batch upload failed: {e}")
            result["failed"] = len(entries) - result["success"]

        logger.info(
            f"Upload complete: {result['success']} success, {result['failed']} failed"
        )
        return result

    async def _navigate_to_grade_center(self, course_id: str) -> None:
        page = await self._get_page()
        base_url = self.auth.base_url
        course_url = (
            f"{base_url}/webapps/blackboard/execute/launcher?type=Course&id={course_id}"
        )

        logger.info(f"Navigating to course: {course_id}")
        await page.goto(course_url, wait_until="domcontentloaded", timeout=self.upload_timeout)
        await page.wait_for_timeout(2000)

        try:
            grade_center_link = page.locator(SEL_GRADE_CENTER_LINK).first
            await grade_center_link.wait_for(state="visible", timeout=10000)
            await grade_center_link.click()
            await page.wait_for_timeout(1500)
        except Exception as e:
            logger.error(f"Failed to find Grade Center link: {e}")
            raise

        try:
            full_gc_link = page.locator(SEL_FULL_GRADE_CENTER).first
            await full_gc_link.wait_for(state="visible", timeout=10000)
            await full_gc_link.click()
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"Full Grade Center link not found, trying direct content: {e}")

        self._content_frame = await self._get_content_frame(page)
        logger.info("Navigated to Full Grade Center")

    async def _get_content_frame(self, page: Page) -> Frame:
        try:
            frame_element = page.frame_locator(SEL_CONTENT_FRAME).first
            await frame_element.locator("body").wait_for(timeout=5000)
            frame = page.frame(url=lambda u: "gradebook" in u or "GradeCenter" in u)
            if frame:
                return frame
        except Exception:
            pass

        for frame in page.frames:
            if any(
                keyword in (frame.url or "")
                for keyword in ["gradebook", "GradeCenter", "enterGradeCenter"]
            ):
                return frame

        return page.main_frame

    async def _find_column(self, column_name: str) -> Optional[int]:
        frame = self._content_frame
        if frame is None:
            raise RuntimeError("Not navigated to Grade Center")

        try:
            headers = frame.locator(SEL_GRADE_COLUMN_HEADER)
            count = await headers.count()

            for i in range(count):
                header = headers.nth(i)
                text = await header.text_content()
                if text and column_name.strip().lower() in text.strip().lower():
                    logger.info(f"Found column '{column_name}' at index {i}")
                    return i

            all_headers = frame.locator("th")
            count = await all_headers.count()
            for i in range(count):
                header = all_headers.nth(i)
                text = await header.text_content()
                if text and column_name.strip().lower() in text.strip().lower():
                    logger.info(f"Found column '{column_name}' at index {i} (fallback)")
                    return i

        except Exception as e:
            logger.error(f"Error finding column '{column_name}': {e}")

        return None

    async def _find_student_row(self, student_id: str) -> Optional[int]:
        frame = self._content_frame
        if frame is None:
            raise RuntimeError("Not navigated to Grade Center")

        try:
            rows = frame.locator("table tbody tr")
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()

                for j in range(min(cell_count, 5)):
                    cell = cells.nth(j)
                    text = await cell.text_content()
                    if text and student_id.strip() in text.strip():
                        logger.info(f"Found student '{student_id}' at row {i}")
                        return i

        except Exception as e:
            logger.error(f"Error finding student '{student_id}': {e}")

        return None

    async def _enter_score(self, row: int, column: int, score: float) -> None:
        frame = self._content_frame
        if frame is None:
            raise RuntimeError("Not navigated to Grade Center")

        grade_offset = 3
        target_col = column + grade_offset

        try:
            cell = frame.locator(f"table tbody tr:nth-child({row + 1}) td:nth-child({target_col + 1})")
            await cell.wait_for(state="visible", timeout=5000)
            await cell.click()
            await frame.page.wait_for_timeout(self.cell_delay)

            grade_input = cell.locator(SEL_GRADE_INPUT)
            try:
                await grade_input.wait_for(state="visible", timeout=3000)
                await grade_input.fill(str(score))
            except Exception:
                await frame.page.keyboard.type(str(score))

            await frame.page.wait_for_timeout(self.cell_delay)
            await frame.page.keyboard.press("Tab")
            await frame.page.wait_for_timeout(self.cell_delay)

            logger.debug(f"Entered score {score} at row {row}, col {target_col}")

        except Exception as e:
            logger.error(f"Error entering score at row {row}, col {target_col}: {e}")
            raise

    async def _save_changes(self) -> None:
        frame = self._content_frame
        if frame is None:
            return

        try:
            save_btn = frame.locator(SEL_SAVE_BUTTON).first
            if await save_btn.count() > 0 and await save_btn.is_visible():
                await save_btn.click()
                await frame.page.wait_for_timeout(2000)
                logger.info("Changes saved")
        except Exception as e:
            logger.warning(f"Save button not found or click failed: {e}")

    async def _verify_upload(
        self, student_id: str, column_name: str, expected_score: float
    ) -> bool:
        frame = self._content_frame
        if frame is None:
            return False

        try:
            await frame.page.wait_for_timeout(1000)

            row = await self._find_student_row(student_id)
            if row is None:
                return False

            col_index = await self._find_column(column_name)
            if col_index is None:
                return False

            grade_offset = 3
            target_col = col_index + grade_offset

            cell = frame.locator(
                f"table tbody tr:nth-child({row + 1}) td:nth-child({target_col + 1})"
            )
            text = await cell.text_content()

            if text is None:
                return False

            clean_text = text.strip().replace("%", "")
            try:
                actual_score = float(clean_text)
                if abs(actual_score - expected_score) < 0.01:
                    logger.info(
                        f"Verified: student '{student_id}' score = {actual_score}"
                    )
                    return True
            except ValueError:
                pass

            logger.warning(
                f"Verification mismatch for '{student_id}': "
                f"expected {expected_score}, got '{text.strip()}'"
            )
            return False

        except Exception as e:
            logger.error(f"Verification error for '{student_id}': {e}")
            return False
