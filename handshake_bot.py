import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

APPLIED_JOBS_FILE = Path("applied_jobs.json")
CONFIG_FILE = Path("handshake_config.json")

HANDSHAKE_BASE = "https://app.joinhandshake.com"
LOGIN_URL = f"{HANDSHAKE_BASE}/stu/users/sign_in"
JOBS_URL = f"{HANDSHAKE_BASE}/stu/jobs"


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy handshake_config.example.json to handshake_config.json and fill in your details."
        )
    with open(path) as f:
        return json.load(f)


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


class HandshakeBot:
    def __init__(self, config: dict):
        filters = config.get("filters", {})
        settings = config.get("settings", {})

        self.email: str = config["email"]
        self.password: str = config["password"]
        self.job_keywords: list = filters.get("job_keywords", [])
        self.locations: list = filters.get("locations", [])
        self.company_allowlist: list = [c.lower() for c in filters.get("company_allowlist", [])]
        self.company_blocklist: list = [c.lower() for c in filters.get("company_blocklist", [])]

        self.headless: bool = settings.get("headless", True)
        self.max_applications: int = settings.get("max_applications_per_run", 10)
        self.delay: float = settings.get("delay_between_applications_seconds", 3.0)

        self.tracker = AppliedJobsTracker()
        self.session_results: list = []

    async def _login(self, page: Page) -> None:
        logger.info("Navigating to Handshake login...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Fill email — selector covers standard input and Handshake's labelled input
        email_input = page.locator(
            'input[type="email"], input[name="email"], input[placeholder*="email" i]'
        ).first
        await email_input.wait_for(state="visible", timeout=10000)
        await email_input.fill(self.email)
        await email_input.press("Enter")
        await page.wait_for_timeout(2000)

        # Password may appear after the email step (email-first flow)
        password_input = page.locator('input[type="password"]').first
        await password_input.wait_for(state="visible", timeout=10000)
        await password_input.fill(self.password)
        await password_input.press("Enter")

        # Wait for post-login redirect
        try:
            await page.wait_for_url(f"{HANDSHAKE_BASE}/**", timeout=15000)
        except Exception:
            pass

        await page.wait_for_timeout(2000)

        if "sign_in" in page.url or "login" in page.url:
            err_el = page.locator('[class*="error" i], [class*="alert" i], [role="alert"]').first
            err_text = ""
            try:
                err_text = await err_el.text_content(timeout=2000) or ""
            except Exception:
                pass
            raise RuntimeError(
                f"Login failed. URL still: {page.url}. "
                f"Page error: {err_text.strip() or 'none detected'}. "
                "Check your credentials in handshake_config.json."
            )

        logger.info("Logged in successfully.")

    def _passes_filters(self, title: str, company: str, job_id: str) -> tuple:
        if self.tracker.has_applied(job_id):
            return False, "already applied"

        company_lower = company.lower()

        if self.company_blocklist and any(bl in company_lower for bl in self.company_blocklist):
            return False, f"'{company}' is blocklisted"

        if self.company_allowlist and not any(al in company_lower for al in self.company_allowlist):
            return False, f"'{company}' not in allowlist"

        return True, "ok"

    async def _search_jobs(self, page: Page, keyword: str, location: Optional[str]) -> list:
        params = {"query": keyword}
        if location:
            params["location"] = location
        qs = "&".join(f"{k}={v}" for k, v in params.items())

        await page.goto(f"{JOBS_URL}?{qs}", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Try multiple selector strategies — Handshake's class names can change
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
                f"No job cards found for '{keyword}'. "
                "Handshake may have changed its markup — check selectors in handshake_bot.py."
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

                text = await card.inner_text()
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                title = lines[0] if lines else "Unknown"
                company = lines[1] if len(lines) > 1 else "Unknown"

                jobs.append({"id": job_id, "title": title, "company": company, "url": full_url})
            except Exception as exc:
                logger.debug(f"Error parsing card: {exc}")

        label = f"'{keyword}'" + (f" in '{location}'" if location else "")
        logger.info(f"Found {len(jobs)} jobs for {label}")
        return jobs

    async def _apply(self, page: Page, job: dict) -> dict:
        result = {
            "job_id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "url": job["url"],
            "status": "failed",
            "timestamp": datetime.now().isoformat(),
        }

        try:
            await page.goto(job["url"], wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

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

            # Detect redirect to external company ATS
            if HANDSHAKE_BASE not in page.url:
                result["status"] = "external_redirect"
                result["external_url"] = page.url
                logger.info(f"External ATS for '{job['title']}': {page.url}")
                return result

            # Quick Apply modal — look for a submit/confirm button
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

        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            logger.exception(f"Error applying to '{job['title']}': {exc}")

        return result

    async def run(self) -> list:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            try:
                await self._login(page)

                sent = 0
                search_locations = self.locations if self.locations else [None]

                for keyword in self.job_keywords:
                    if sent >= self.max_applications:
                        break
                    for location in search_locations:
                        if sent >= self.max_applications:
                            break

                        jobs = await self._search_jobs(page, keyword, location)

                        for job in jobs:
                            if sent >= self.max_applications:
                                break

                            ok, reason = self._passes_filters(job["title"], job["company"], job["id"])
                            if not ok:
                                logger.info(f"Skipping '{job['title']}' ({reason})")
                                continue

                            result = await self._apply(page, job)
                            self.session_results.append(result)

                            if result["status"] == "applied":
                                self.tracker.mark_applied(job["id"], job["title"], job["company"])
                                sent += 1

                            await asyncio.sleep(self.delay)

            finally:
                await browser.close()

        applied_count = sum(1 for r in self.session_results if r["status"] == "applied")
        logger.info(f"Session done. Applied to {applied_count} job(s).")
        return self.session_results


async def main():
    config = load_config()
    bot = HandshakeBot(config)
    results = await bot.run()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
