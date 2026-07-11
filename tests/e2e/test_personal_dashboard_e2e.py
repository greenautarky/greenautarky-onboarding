"""E2E (Playwright) — the personal-dashboard use-case through the real UI.

Drives a real browser against a live device along the REAL join-wizard flow
(verified step-by-step on K0, 2026-07-11):

1. ``/greenautarky-join`` redirects to ``/greenautarky-setup.html?join=1``
   (Lit wizard). The PIN step is a single textbox (placeholder ``000-000``)
   that AUTO-advances once 6 digits are entered.
2. Step "Benutzerkonto erstellen": Datenschutz checkbox (mwc — needs a
   forced check, the ripple animation keeps it "unstable"), then the
   ha-form name/password fields, then "Konto erstellen".
3. The master's manage API then lists the new sub-user WITH an
   auto-assigned ``ga-home-*`` personal dashboard, and the board URL serves.

KNOWN FAILURE (Odoo #512): on bundles built against a mismatched HA
frontend, the ha-form fields never render (dynamic ``/frontend_latest/*.js``
chunk imports 404) — this test's "account form renders" assert is the
regression gate for exactly that bug.

All API calls go through Playwright's request context (node-side network,
unaffected by pytest_socket). Skipped unless env is set; CANARIES ONLY;
cleans up the throwaway sub-user via the master API.

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=... GA_DEVICE_MASTER_PASSWORD=... \
    pytest tests/e2e -m e2e
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

CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"


async def _master_token(request_ctx) -> str:
    """HA login flow → access token, via Playwright's request context."""
    r = await request_ctx.post(
        "/auth/login_flow",
        data={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    )
    assert r.ok, await r.text()
    flow = await r.json()
    r = await request_ctx.post(
        f"/auth/login_flow/{flow['flow_id']}",
        data={
            "client_id": CLIENT_ID,
            "username": MASTER_USERNAME,
            "password": MASTER_PASSWORD,
        },
    )
    assert r.ok, await r.text()
    result = await r.json()
    code = result.get("result")
    assert code, f"login flow did not finish: {result}"
    r = await request_ctx.post(
        "/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
        },
    )
    assert r.ok, await r.text()
    return (await r.json())["access_token"]


@requires_device
async def test_invite_join_and_personal_dashboard(socket_enabled) -> None:
    marker = uuid.uuid4().hex[:6]
    sub_name = f"E2E Kurz {marker}"
    sub_password = f"pw-{secrets.token_urlsafe(12)}"

    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch()
        api = await pw.request.new_context(base_url=DEVICE_URL)
        token = ""
        sub_user_id = None
        try:
            token = await _master_token(api)
            auth = {"Authorization": f"Bearer {token}"}

            # 1) master issues an invite PIN (API — the console UI wraps this)
            r = await api.post(
                "/api/greenautarky_onboarding/sub_user/invite",
                headers=auth,
                data={},
            )
            assert r.ok, await r.text()
            pin = (await r.json())["pin"]

            # 2) join wizard in a real browser
            page = await (await browser.new_context()).new_page()
            await page.goto(f"{DEVICE_URL}/greenautarky-join")
            pin_box = page.locator('input[placeholder="000-000"]')
            await pin_box.wait_for(timeout=20000)
            await pin_box.fill(pin)  # 6 digits → wizard auto-advances

            # consent step
            consent = page.locator('input[type="checkbox"]').first
            await consent.wait_for(timeout=15000)
            await consent.check(force=True)  # mwc ripple keeps it "unstable"

            # 3) THE #512 REGRESSION GATE: the ha-form account fields must
            # render. On a chunk-mismatched bundle they never appear
            # (dynamic /frontend_latest/*.js imports 404) and the join is
            # dead in the browser even though the API path works.
            name_field = page.locator(
                'input[type="text"], ha-form input:not([type="checkbox"])'
            ).first
            try:
                await name_field.wait_for(timeout=15000)
            except playwright_async.TimeoutError:
                pytest.fail(
                    "Account form fields never rendered on the consent step "
                    "— frontend chunk 404s (Odoo #512). UI join is broken "
                    "for customers on this bundle."
                )

            await name_field.fill(sub_name)
            # two password inputs: Passwort + Passwort bestätigen — both
            # must be filled or "Konto erstellen" stays disabled
            pw_fields = page.locator('input[type="password"]')
            await pw_fields.nth(0).fill(sub_password)
            await pw_fields.nth(1).fill(sub_password)
            await page.get_by_role("button", name="Konto erstellen").click()
            await page.wait_for_load_state("networkidle")

            # 4) the master's surface lists the new sub-user with an
            # auto-created ga-home-* dashboard, and the board URL serves
            r = await api.get(
                "/api/greenautarky_onboarding/sub_user/list", headers=auth
            )
            assert r.ok, await r.text()
            listing = await r.json()
            sub = next(
                s for s in listing["sub_users"] if s.get("name") == sub_name
            )
            sub_user_id = sub["user_id"]
            personal = [
                d for d in (sub.get("dashboards") or [])
                if d.startswith("ga-home-")
            ]
            assert personal, f"no auto-created dashboard: {sub}"
            r = await api.get(f"/{personal[0]}", headers=auth)
            assert r.ok, f"board {personal[0]} → {r.status}"
        finally:
            if sub_user_id:
                await api.post(
                    "/api/greenautarky_onboarding/sub_user/remove",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"sub_user_id": sub_user_id},
                )
            await api.dispose()
            await browser.close()
