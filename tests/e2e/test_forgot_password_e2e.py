"""E2E (Playwright) — the REAL login page's "Forgot password?" (ADR-0040).

The browser tier in ``tests/browser`` runs the script against a fake page that
models ``ha-auth-flow``. This drives Home Assistant's actual login page on a
device: if a Core/frontend update renamed the link's class or moved it into a
shadow root, only this test notices.

The second test asserts that nothing on the page links to home-assistant.io
(the footer's "Help" button included, ADR-0040 D7).

Needs NO credentials and submits nothing — it opens the login page, follows
the link, and stops on the PIN step. Needs a device that HAS an onboarding PIN
(every GA-provisioned device); without one the stock link is expected, and the
test says so instead of passing.

    GA_DEVICE_URL=http://<device-ip>:8123 pytest tests/e2e -m e2e -k forgot

CANARIES ONLY.
"""

from __future__ import annotations

import os

import pytest

playwright_async = pytest.importorskip(
    "playwright.async_api", reason="playwright not installed"
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

DEVICE_URL = os.environ.get("GA_DEVICE_URL", "").rstrip("/")
RESET_PAGE_URL = "/greenautarky-password-reset"

requires_device = pytest.mark.skipif(not DEVICE_URL, reason="GA_DEVICE_URL not set")


@requires_device
async def test_forgot_password_on_the_real_login_page_opens_the_pin_reset() -> None:
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(locale="de-DE")
            page = await context.new_page()
            await page.goto(
                f"{DEVICE_URL}/auth/authorize?response_type=code"
                f"&client_id={DEVICE_URL}/&redirect_uri={DEVICE_URL}/?auth_callback=1"
            )
            link = page.locator("a.forgot-password")
            await link.wait_for(timeout=30_000)

            href = await link.get_attribute("href")
            assert href == RESET_PAGE_URL, (
                f"'Forgot password?' still points at {href!r}. Either the device "
                "has no onboarding PIN (then this is expected — check "
                ".storage/greenautarky_secrets/onboarding_pin), or the patch did "
                "not reach /auth/authorize (Core log: 'greenautarky_site: "
                "/auth/authorize')"
            )

            await link.click()
            await page.wait_for_url(f"{DEVICE_URL}{RESET_PAGE_URL}")
            await page.locator("#step-pin").wait_for()
            assert len(context.pages) == 1, "the reset page opened in a new tab"
        finally:
            await browser.close()


@requires_device
async def test_nothing_on_the_real_login_page_links_to_home_assistant_io() -> None:
    """ADR-0040 D7: neither "Forgot password?" nor the footer's Help button (nor
    anything a future frontend adds) sends a resident to home-assistant.io.
    Playwright's CSS engine pierces open shadow roots, so the anchor inside an
    ``ha-button`` counts too."""
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await (await browser.new_context(locale="de-DE")).new_page()
            await page.goto(
                f"{DEVICE_URL}/auth/authorize?response_type=code"
                f"&client_id={DEVICE_URL}/&redirect_uri={DEVICE_URL}/?auth_callback=1"
            )
            # Wait until the page is rendered AND our script has run: the
            # retargeted link is the signal. Counting earlier would pass on a
            # page that has not drawn its footer yet.
            await page.wait_for_selector(
                f"a.forgot-password[href='{RESET_PAGE_URL}']", timeout=30_000
            )
            await page.wait_for_load_state("networkidle")
            offenders = page.locator("[href*='home-assistant.io']")
            hrefs = [await offenders.nth(i).get_attribute("href")
                     for i in range(await offenders.count())]
            assert not hrefs, f"the login page still links to home-assistant.io: {hrefs}"
        finally:
            await browser.close()
