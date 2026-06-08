import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

DEFAULT_BB_URL = "https://elearning.shanghaitech.edu.cn:8443"
DEFAULT_SSO_URL = "https://ids.shanghaitech.edu.cn/authserver/login"
SESSION_FILE = "session.json"

SEL_USERNAME = "#username"
SEL_PASSWORD = "#password"
SEL_SUBMIT_BUTTONS = ["#dlsubmit", "button[type='submit']", ".login-btn", "input[type='submit']"]

SEL_MFA_INDICATORS = [
    "#captcha", "#verifyCode", "#authCode",
    ".captcha-img", ".verify-code", "#mfa",
    "img[alt*='captcha']", "img[alt*='验证码']",
]

BB_DOMAIN = "elearning.shanghaitech.edu.cn"
SSO_DOMAIN = "ids.shanghaitech.edu.cn"


class BlackboardAuth:

    def __init__(
        self,
        base_url: str = DEFAULT_BB_URL,
        sso_url: str = DEFAULT_SSO_URL,
        username: str = "",
        password: str = "",
        headless: bool = False,
        slow_mo: int = 0,
    ):
        self.base_url = base_url.rstrip("/")
        self.sso_url = sso_url
        self.username = username
        self.password = password
        self.headless = headless
        self.slow_mo = slow_mo

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def _ensure_browser(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        return self._page

    async def login(self) -> bool:
        if not self.username or not self.password:
            logger.error("Username and password are required for login")
            return False

        page = await self._ensure_browser()
        logger.info(f"Navigating to Blackboard: {self.base_url}")

        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.error(f"Failed to navigate to Blackboard: {e}")
            return False

        await self._wait_for_sso_page(page)

        current_url = page.url
        if SSO_DOMAIN not in current_url:
            if BB_DOMAIN in current_url:
                logger.info("Already authenticated, no SSO redirect")
                return True
            logger.warning(f"Unexpected URL after navigation: {current_url}")
            return False

        logger.info("On SSO login page, filling credentials")
        return await self._fill_and_submit_login(page)

    async def _wait_for_sso_page(self, page: Page, timeout: int = 15000) -> None:
        try:
            await page.wait_for_function(
                f"""() => {{
                    const url = window.location.href;
                    return url.includes('{SSO_DOMAIN}') || url.includes('{BB_DOMAIN}');
                }}""",
                timeout=timeout,
            )
        except Exception:
            logger.debug("Timeout waiting for page transition, proceeding with current state")

    async def _fill_and_submit_login(self, page: Page) -> bool:
        try:
            await page.wait_for_selector(SEL_USERNAME, timeout=10000)
        except Exception:
            logger.error("Username field not found on SSO page")
            return False

        await page.fill(SEL_USERNAME, "")
        await page.type(SEL_USERNAME, self.username, delay=50)

        await page.fill(SEL_PASSWORD, "")
        await page.type(SEL_PASSWORD, self.password, delay=50)

        if await self._detect_mfa_or_captcha(page):
            logger.warning("MFA/Captcha detected — waiting for manual intervention...")
            await self._wait_for_manual_input(page)

        submitted = False
        for selector in SEL_SUBMIT_BUTTONS:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    submitted = True
                    logger.info(f"Clicked login button: {selector}")
                    break
            except Exception:
                continue

        if not submitted:
            logger.info("No submit button found, pressing Enter")
            await page.press(SEL_PASSWORD, "Enter")

        return await self._wait_for_bb_redirect(page)

    async def _detect_mfa_or_captcha(self, page: Page) -> bool:
        for selector in SEL_MFA_INDICATORS:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_manual_input(self, page: Page, timeout: int = 120000) -> None:
        logger.info("Please complete MFA/captcha in the browser window...")
        try:
            await page.wait_for_function(
                f"() => !window.location.href.includes('{SSO_DOMAIN}/authserver/login')",
                timeout=timeout,
            )
        except Exception:
            logger.warning("Manual input timeout — proceeding anyway")

    async def _wait_for_bb_redirect(self, page: Page, timeout: int = 30000) -> bool:
        try:
            await page.wait_for_function(
                f"() => window.location.href.includes('{BB_DOMAIN}')",
                timeout=timeout,
            )
            logger.info("Successfully redirected to Blackboard")
            return True
        except Exception:
            current_url = page.url
            logger.error(f"Login redirect timeout. Current URL: {current_url}")
            if SSO_DOMAIN in current_url:
                error_msg = await self._extract_sso_error(page)
                if error_msg:
                    logger.error(f"SSO error: {error_msg}")
            return False

    async def _extract_sso_error(self, page: Page) -> Optional[str]:
        for selector in [".error-msg", "#errorMsg", ".login-error", "#msg"]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.text_content()
                    if text and text.strip():
                        return text.strip()
            except Exception:
                continue
        return None

    async def save_session(self, path: str = SESSION_FILE) -> bool:
        if not self._context:
            logger.error("No active browser context to save")
            return False

        try:
            storage = await self._context.storage_state()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(storage, f, indent=2)
            logger.info(f"Session saved to {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            return False

    async def load_session(self, path: str = SESSION_FILE) -> bool:
        if not Path(path).exists():
            logger.debug(f"No session file at {path}")
            return False

        try:
            with open(path) as f:
                storage_state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read session file: {e}")
            return False

        await self.close()

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self._context = await self._browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
        )
        self._page = await self._context.new_page()

        if await self.is_authenticated():
            logger.info("Loaded session is valid")
            return True

        logger.info("Loaded session has expired")
        return False

    async def is_authenticated(self) -> bool:
        if not self._page or self._page.is_closed():
            return False

        try:
            await self._page.goto(self.base_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            current_url = self._page.url

            if SSO_DOMAIN in current_url:
                logger.debug("Session expired — redirected to SSO")
                return False

            if BB_DOMAIN in current_url:
                try:
                    await self._page.wait_for_selector("#myCourses", timeout=5000)
                    return True
                except Exception:
                    pass
                login_form = await self._page.query_selector(SEL_USERNAME)
                if login_form:
                    return False
                return True

            return False

        except Exception as e:
            logger.error(f"Auth check failed: {e}")
            return False

    async def close(self) -> None:
        for resource_attr in ("_page", "_context", "_browser", "_playwright"):
            resource = getattr(self, resource_attr)
            if resource is None:
                continue
            try:
                if resource_attr == "_playwright":
                    await resource.stop()
                elif resource_attr == "_page" and resource.is_closed():
                    continue
                else:
                    await resource.close()
            except Exception:
                pass

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.debug("Browser closed")

    async def get_page(self, session_path: str = SESSION_FILE) -> Page:
        if self._page and not self._page.is_closed():
            if await self.is_authenticated():
                return self._page

        if await self.load_session(session_path):
            return self._page

        logger.info("No valid session, performing fresh login")
        if not await self.login():
            raise RuntimeError("Authentication failed — check credentials")

        await self.save_session(session_path)
        return self._page
