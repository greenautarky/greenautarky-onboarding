"""E2E (Playwright) — the personal-dashboard use-case through the real UI.

Drives a real browser against a live device: the master opens the
master console, issues an invite PIN, the sub-user joins on the join
page, logs in, and SEES their auto-created personal dashboard render.
This is the highest-level release test — it exercises component + Core +
frontend bundle + auth exactly as a customer would.

Skipped unless env is set (same vars as the device tests) AND playwright
is installed (``pip install playwright && playwright install chromium``):

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=... GA_DEVICE_MASTER_PASSWORD=... \
    pytest tests/e2e -m e2e

CANARIES ONLY. Cleans up the throwaway sub-user via the master API.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest

playwright_async = pytest.importorskip(
    "playwright.async_api", reason="playwright not installed"
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

DEVICE_URL = os.environ.get("GA_DEVICE_URL", "").rstrip("/")
MASTER_USERNAME = os.environ.get("GA_DEVICE_MASTER_USERNAME", "")
MASTER_PASSWORD = os.environ.get("GA_DEVICE_MASTER_PASSWORD", "")

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / master credentials not set",
)


async def _ha_login(page, username: str, password: str) -> None:
    """Log in through HA's real login form."""
    await page.goto(f"{DEVICE_URL}/")
    await page.wait_for_selector("input[name=username], ha-textfield", timeout=20000)
    await page.fill("input[name=username]", username)
    await page.fill("input[name=password]", password)
    await page.keyboard.press("Enter")
    await page.wait_for_url(f"{DEVICE_URL}/**", timeout=20000)


@requires_device
async def test_invite_join_and_see_personal_dashboard() -> None:
    marker = uuid.uuid4().hex[:6]
    sub_name = f"E2E Kurz {marker}"
    sub_password = f"pw-{secrets.token_urlsafe(12)}"

    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        sub_user_id = None
        master_ctx = await browser.new_context()
        try:
            # --- master: console → invite PIN --------------------------------
            master_page = await master_ctx.new_page()
            await _ha_login(master_page, MASTER_USERNAME, MASTER_PASSWORD)
            await master_page.goto(f"{DEVICE_URL}/greenautarky-master")
            await master_page.click("text=Einladungs-PIN erzeugen")
            pin_el = await master_page.wait_for_selector(
                "code, .pin, [data-pin]", timeout=15000
            )
            pin = (await pin_el.inner_text()).strip()
            assert pin, "no invite PIN rendered"

            # --- sub-user: join page ------------------------------------------
            join_ctx = await browser.new_context()
            join_page = await join_ctx.new_page()
            await join_page.goto(f"{DEVICE_URL}/greenautarky-join")
            await join_page.fill(
                "input[name=name], #name, input[placeholder*=Name]", sub_name
            )
            await join_page.fill(
                "input[type=password]", sub_password
            )
            await join_page.fill(
                "input[name=invite_pin], #invite_pin, input[placeholder*=PIN]", pin
            )
            consent = join_page.locator("input[type=checkbox]").first
            if await consent.count():
                await consent.check()
            await join_page.click("button[type=submit], text=Beitreten")
            await join_page.wait_for_load_state("networkidle")

            # --- sub-user: login, personal dashboard renders ------------------
            sub_ctx = await browser.new_context()
            sub_page = await sub_ctx.new_page()
            await _ha_login(sub_page, sub_name.lower().replace(" ", ""), sub_password)
            # the auto-created board greets the user by name
            await sub_page.wait_for_selector(
                f"text=Willkommen, {sub_name}", timeout=30000
            )
        finally:
            # cleanup via master API token (page context carries auth)
            try:
                cleanup = await master_ctx.new_page()
                listing = await cleanup.evaluate(
                    """async () => {
                        const t = JSON.parse(
                            localStorage.getItem('hassTokens')).access_token;
                        const r = await fetch(
                            '/api/greenautarky_onboarding/sub_user/list',
                            {headers: {Authorization: 'Bearer ' + t}});
                        return await r.json();
                    }"""
                )
                for sub in listing.get("sub_users", []):
                    if sub.get("name") == sub_name:
                        sub_user_id = sub["user_id"]
                if sub_user_id:
                    await cleanup.evaluate(
                        """async (uid) => {
                            const t = JSON.parse(
                                localStorage.getItem('hassTokens')).access_token;
                            await fetch(
                                '/api/greenautarky_onboarding/sub_user/remove',
                                {method: 'POST',
                                 headers: {Authorization: 'Bearer ' + t,
                                           'Content-Type': 'application/json'},
                                 body: JSON.stringify({sub_user_id: uid})});
                        }""",
                        sub_user_id,
                    )
            finally:
                await browser.close()
