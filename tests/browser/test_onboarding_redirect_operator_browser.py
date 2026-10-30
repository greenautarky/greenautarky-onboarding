"""ga-onboarding-redirect.js in a real browser — operator bypass of the wizard.

K31, 2026-10-30: after a clean flash (wizard open) the fleet-manager's "Admin"
button landed on /greenautarky-setup.html instead of the admin area, although
the fm promised "works whether or not onboarding is finished". The redirect is
client-side; only a browser can prove it. Playwright answers every request
itself, nothing leaves the machine. The script under test is the LIVE file.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

if os.environ.get("GA_REQUIRE_BROWSER"):
    import playwright.async_api as playwright_async
else:
    playwright_async = pytest.importorskip("playwright.async_api", reason="playwright not installed")

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]

ORIGIN = "http://ga-device.test"
SCRIPT = (pathlib.Path(__file__).resolve().parents[2]
          / "src/greenautarky_site/frontend_bundle/ga-onboarding-redirect.js").read_text()
PAGE = '<!DOCTYPE html><html><body><p id="ha">HA admin</p><script src="/redirect.js"></script></body></html>'
SETUP = '<!DOCTYPE html><html><body><p id="setup">wizard</p></body></html>'


async def _land(marker_age_ms: int | None, completed: bool) -> str:
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()

        async def route(r):
            url = r.request.url
            if url.endswith("/redirect.js"):
                await r.fulfill(body=SCRIPT, content_type="application/javascript")
            elif "/api/greenautarky_site/status" in url:
                await r.fulfill(body=json.dumps({"completed": completed}), content_type="application/json")
            elif "/greenautarky-setup.html" in url:
                await r.fulfill(body=SETUP, content_type="text/html")
            else:
                await r.fulfill(body=PAGE, content_type="text/html")

        await page.route("**/*", route)
        await page.goto(f"{ORIGIN}/blank")
        if marker_age_ms is not None:
            await page.evaluate(
                "age => localStorage.setItem('ga_operator_session', JSON.stringify({at: Date.now() - age}))",
                marker_age_ms)
        await page.goto(f"{ORIGIN}/config/dashboard")
        await page.wait_for_timeout(800)
        path = page.url.replace(ORIGIN, "")
        await browser.close()
        return path


async def test_operator_with_fresh_marker_stays_in_the_admin_area():
    assert await _land(marker_age_ms=60_000, completed=False) == "/config/dashboard"


async def test_without_marker_the_open_wizard_still_redirects():
    """Must-fail guard: the resident's wizard is untouched."""
    assert (await _land(marker_age_ms=None, completed=False)).startswith("/greenautarky-setup.html")


async def test_a_stale_marker_no_longer_bypasses():
    assert (await _land(marker_age_ms=13 * 3600 * 1000, completed=False)).startswith("/greenautarky-setup.html")


async def test_finished_onboarding_never_redirects():
    assert await _land(marker_age_ms=None, completed=True) == "/config/dashboard"
