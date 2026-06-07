import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

APPLIED_JOBS_FILE = Path("applied_jobs.json")
CONFIG_FILE = Path("handshake_config.json")
COOKIES_FILE = Path("handshake_cookies.json")

HANDSHAKE_BASE = "https://app.joinhandshake.com"
JOBS_URL = f"{HANDSHAKE_BASE}/stu/jobs"

# Phrases that confirm visa sponsorship is offered
VISA_POSITIVE = [
    r"visa sponsorship",
    r"will sponsor",
    r"h[\-\s]?1[\-\s]?b",
    r"sponsorship (is |will be |)provided",
    r"sponsor(s)? (work |employment |visa )",
    r"work (visa|authorization) (provided|sponsored|available)",
    r"opt[/ ]cpt",
]

# Phrases that explicitly rule out sponsorship
VISA_NEGATIVE = [
    r"no visa sponsorship",
    r"not (able |)(offer|provide|sponsor|support) (visa |work |)sponsor",
    r"cannot (offer |provide |)sponsor",
    r"will not sponsor",
    r"does not (offer|provide) sponsor",
    r"sponsorship (is |)not (available|offered|provided)",
    r"must be (legally |)(authorized|eligible) to work",
    r"not (eligible|available) for (visa )?sponsor",
    r"unable to sponsor",
    r"no sponsorship",
]


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy handshake_config.example.json to handshake_config.json and fill in your details."
        )
    with open(path) as f:
        return json.load(f)


def _normalize_cookies(raw: list) -> list:
    """Convert Cookie-Editor JSON export format to Playwright's format."""
    SAME_SITE_MAP = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
        "unspecified": "None",
        "none": "None",
    }
    result = []
    for c in raw:
        domain = c.get("domain", "")
        if "joinhandshake.com" not in domain:
            continue
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": SAME_SITE_MAP.get(c.get("sameSite", "").lower(), "None"),
        }
        if "expirationDate" in c:
            cookie["expires"] = int(c["expirationDate"])
        result.append(cookie)
    return result


class AppliedJobsTracker:
    def __init__(self, path: Path = APPLIED_JOBS_FILE):
        self.path = path
        self.data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path) as f:
                return json.load(f)
        return {}

    def has_applied(self, job_id: str) -> bool:
        return job_id in self.data

    def mark_applied(self, job_id: str, title: str, company: str) -> None:
        self.data[job_id] = {
            "title": title,
            "company": company,
            "applied_at": datetime.now().isoformat(),
        }
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def all_applications(self) -> list:
        return [{"id": k, **v} for k, v in self.data.items()]


def _check_sponsorship_in_text(text: str) -> tuple:
    """
    Returns (sponsored: bool | None, reason: str).
    None means the job description didn't mention sponsorship either way.
    """
    text = text.lower()
    for pattern in VISA_NEGATIVE:
        if re.search(pattern, text):
            return False, f"no-sponsorship phrase matched: '{pattern}'"
    for pattern in VISA_POSITIVE:
        if re.search(pattern, text):
            return True, f"sponsorship phrase matched: '{pattern}'"
    return None, "sponsorship not mentioned"


def _print_cookie_instructions() -> None:
    print("\n" + "=" * 62)
    print("  SETUP REQUIRED — export your Handshake session cookies")
    print()
    print("  Do this once (repeat only when your session expires):")
    print()
    print("  1. Install the 'Cookie-Editor' extension in Chrome:")
    print("     https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm")
    print()
    print("  2. In Chrome, go to https://app.joinhandshake.com and")
    print("     make sure you are fully logged in.")
    print()
    print("  3. Click the Cookie-Editor toolbar icon.")
    print("     Click  Export  →  Export as JSON")
    print()
    print(f"  4. Save the file as:  {(Path.cwd() / 'handshake_cookies.json').resolve()}")
    print()
    print("  5. Run the bot again.")
    print("=" * 62 + "\n")


class HandshakeBot:
    def __init__(self, config: dict):
        self.config = config
        filters = config.get("filters", {})
        settings = config.get("settings", {})

        self.job_keywords: list = filters.get("job_keywords", [
            "Software Engineer",
            "Hardware Engineer",
            "Electrical Engineer",
            "Mechanical Engineer",
            "Data Engineer",
            "Machine Learning Engineer",
        ])
        self.locations: list = filters.get("locations", ["United States"])
        self.company_blocklist: list = [c.lower() for c in filters.get("company_blocklist", [])]

        self.require_explicit_sponsorship: bool = settings.get("require_explicit_sponsorship", False)
        self.max_applications: int = settings.get("max_applications_per_run", 10)
        self.delay: float = settings.get("delay_between_applications_seconds", 4.0)

        self.tracker = AppliedJobsTracker()
        self.session_results: list = []

    # ------------------------------------------------------------------
    # Auth — cookie-based, never visits the /access login page
    # ------------------------------------------------------------------

    async def _login(self, page: Page) -> None:
        if not COOKIES_FILE.exists():
            _print_cookie_instructions()
            raise RuntimeError(
                "handshake_cookies.json not found. "
                "Follow the instructions above to export your cookies."
            )

        with open(COOKIES_FILE) as f:
            raw = json.load(f)

        cookies = _normalize_cookies(raw)
        if not cookies:
            raise RuntimeError(
                "No joinhandshake.com cookies found in handshake_cookies.json. "
                "Make sure you exported cookies while on the Handshake site."
            )

        await page.context.add_cookies(cookies)

        # Navigate directly to jobs — never touch the /access login page
        await page.goto(JOBS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        if "access" in page.url or "sign_in" in page.url or "login" in page.url:
            _print_cookie_instructions()
            raise RuntimeError(
                "Cookies are expired or invalid — Handshake redirected to login. "
                "Export fresh cookies and try again."
            )

        logger.info("Loaded session cookies — logged in successfully.")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def _apply_sponsorship_filter(self, page: Page) -> None:
        try:
            cb = page.locator(
                'label:has-text("Visa Sponsorship"), '
                'label:has-text("visa sponsorship"), '
                '[data-hook*="sponsorship"], '
                'input[value*="sponsorship" i]'
            ).first
            if await cb.is_visible(timeout=3000):
                await cb.click()
                await page.wait_for_timeout(1500)
                logger.info("Activated Handshake visa sponsorship filter.")
        except Exception:
            pass

    async def _search_jobs(self, page: Page, keyword: str, location: str) -> list:
        qs = f"query={keyword}&location={location}"
        await page.goto(f"{JOBS_URL}?{qs}", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        await self._apply_sponsorship_filter(page)

        cards = []
        for selector in [
            '[data-hook="jobs-card"]',
            '[class*="job-card"]',
            'li[class*="job"]',
            'article[class*="job"]',
        ]:
            cards = await page.query_selector_all(selector)
            if cards:
                break

        if not cards:
            logger.warning(
                f"No job cards found for '{keyword}' / '{location}'. "
                "Handshake may have updated its markup — check selectors in handshake_bot.py."
            )
            return []

        jobs = []
        for card in cards:
            try:
                link_el = await card.query_selector("a[href]")
                if not link_el:
                    continue
                href = await link_el.get_attribute("href") or ""
                full_url = f"{HANDSHAKE_BASE}{href}" if href.startswith("/") else href
                job_id = href.split("/")[-1].split("?")[0]
                lines = [ln.strip() for ln in (await card.inner_text()).splitlines() if ln.strip()]
                title = lines[0] if lines else "Unknown"
                company = lines[1] if len(lines) > 1 else "Unknown"
                jobs.append({"id": job_id, "title": title, "company": company, "url": full_url})
            except Exception as exc:
                logger.debug(f"Error parsing card: {exc}")

        logger.info(f"Found {len(jobs)} jobs for '{keyword}' in '{location}'")
        return jobs

    # ------------------------------------------------------------------
    # Sponsorship check
    # ------------------------------------------------------------------

    async def _get_job_description_text(self, page: Page, job_url: str) -> str:
        await page.goto(job_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        return await page.locator("body").inner_text()

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    async def _submit_application(self, page: Page, job: dict) -> dict:
        result = {
            "job_id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "url": job["url"],
            "status": "failed",
            "timestamp": datetime.now().isoformat(),
        }

        apply_btn = page.locator(
            'button:has-text("Apply"), a:has-text("Apply"), '
            '[data-hook*="apply" i], button[class*="apply" i]'
        ).first

        try:
            await apply_btn.wait_for(state="visible", timeout=5000)
        except Exception:
            result["status"] = "no_apply_button"
            return result

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        if HANDSHAKE_BASE not in page.url:
            result["status"] = "external_redirect"
            result["external_url"] = page.url
            logger.info(f"External ATS for '{job['title']}': {page.url}")
            return result

        submit_btn = page.locator(
            'button:has-text("Submit"), button:has-text("Confirm"), '
            'button:has-text("Send Application"), button[type="submit"]'
        ).first

        try:
            await submit_btn.wait_for(state="visible", timeout=4000)
            await submit_btn.click()
            await page.wait_for_timeout(2000)
            result["status"] = "applied"
            logger.info(f"Applied: '{job['title']}' at '{job['company']}'")
        except Exception:
            result["status"] = "form_required"
            logger.info(f"Manual form required: '{job['title']}'")

        return result

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    def _blocked_company(self, company: str) -> bool:
        return any(bl in company.lower() for bl in self.company_blocklist)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> list:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            try:
                await self._login(page)

                sent = 0
                for keyword in self.job_keywords:
                    if sent >= self.max_applications:
                        break
                    for location in self.locations:
                        if sent >= self.max_applications:
                            break

                        jobs = await self._search_jobs(page, keyword, location)

                        for job in jobs:
                            if sent >= self.max_applications:
                                break
                            if self.tracker.has_applied(job["id"]):
                                logger.info(f"Already applied — skipping '{job['title']}'")
                                continue
                            if self._blocked_company(job["company"]):
                                logger.info(f"Blocklisted — skipping '{job['company']}'")
                                continue

                            body_text = await self._get_job_description_text(page, job["url"])
                            sponsored, reason = _check_sponsorship_in_text(body_text)

                            if sponsored is False:
                                logger.info(f"No sponsorship — skipping '{job['title']}': {reason}")
                                self.session_results.append({
                                    **job,
                                    "status": "skipped_no_sponsorship",
                                    "reason": reason,
                                    "timestamp": datetime.now().isoformat(),
                                })
                                continue

                            if sponsored is None and self.require_explicit_sponsorship:
                                logger.info(f"Sponsorship unclear — skipping '{job['title']}': {reason}")
                                self.session_results.append({
                                    **job,
                                    "status": "skipped_sponsorship_unclear",
                                    "reason": reason,
                                    "timestamp": datetime.now().isoformat(),
                                })
                                continue

                            if sponsored is None:
                                logger.info(f"Sponsorship not mentioned — proceeding: '{job['title']}'")

                            result = await self._submit_application(page, job)
                            result["sponsorship_check"] = reason
                            self.session_results.append(result)

                            if result["status"] == "applied":
                                self.tracker.mark_applied(job["id"], job["title"], job["company"])
                                sent += 1

                            await asyncio.sleep(self.delay)

            finally:
                await browser.close()

        applied = sum(1 for r in self.session_results if r["status"] == "applied")
        skipped = sum(1 for r in self.session_results if r["status"].startswith("skipped"))
        logger.info(f"Session done. Applied: {applied}, skipped (no sponsorship): {skipped}.")
        return self.session_results


async def main():
    config = load_config()
    bot = HandshakeBot(config)
    results = await bot.run()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
