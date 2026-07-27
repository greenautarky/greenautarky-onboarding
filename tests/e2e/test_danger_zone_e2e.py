"""E2E (Playwright) — the Danger Zone in a real browser. KB #169.

The card's protection is arming logic, not server code: both destructive
buttons ship ``disabled`` and only become clickable once the confirmation
phrase (and, for the full erase, a six-digit PIN) is typed. Asserting that on
the card's SOURCE — which is all the ga-frontend-bundle suite can do — proves
the string is present, not that the browser behaves. This drives the real
element.

What it checks, on the master's dashboard:

1. the Danger Zone renders in the "Verwalten" view;
2. "Nutzer zurücksetzen" arms only on the exact phrase;
3. "Alles löschen" needs BOTH the phrase and a 6-digit PIN — either alone
   leaves the button dead;
4. the copy keeps its two promises (recorder history is not removed by the
   soft reset; renamed rooms come back as defaults after the full erase).

**Nothing is ever submitted.** Both dialogs are cancelled. The e2e tier must
never fire a wipe — see the K31 record in KB #169 for the accept path.

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=... GA_DEVICE_MASTER_PASSWORD=... \
    pytest tests/e2e -m e2e -k danger_zone

CANARIES ONLY.
"""

from __future__ import annotations

import json
import os

import pytest

playwright_async = pytest.importorskip(
    "playwright.async_api", reason="playwright not installed"
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

DEVICE_URL = os.environ.get("GA_DEVICE_URL", "").rstrip("/")
MASTER_USERNAME = os.environ.get("GA_DEVICE_MASTER_USERNAME", "")
MASTER_PASSWORD = os.environ.get("GA_DEVICE_MASTER_PASSWORD", "")
CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / master credentials not set",
)


async def _token(request_ctx, username: str, password: str) -> str:
    r = await request_ctx.post(
        "/auth/login_flow",
        data={"client_id": CLIENT_ID, "handler": ["homeassistant", None],
              "redirect_uri": CLIENT_ID},
    )
    assert r.ok, await r.text()
    flow = await r.json()
    r = await request_ctx.post(
        f"/auth/login_flow/{flow['flow_id']}",
        data={"client_id": CLIENT_ID, "username": username, "password": password},
    )
    assert r.ok, await r.text()
    result = await r.json()
    code = result.get("result")
    assert code, f"login flow did not finish: {result}"
    r = await request_ctx.post(
        "/auth/token",
        form={"grant_type": "authorization_code", "code": code,
              "client_id": CLIENT_ID},
    )
    assert r.ok, await r.text()
    return (await r.json())["access_token"]


async def _require_tenant_complete_device(request_ctx, token) -> None:
    """Skip unless the device can actually render the Verwalten view.

    Three preconditions, each of which otherwise ends as a confusing
    "card never rendered" timeout (all three hit on the K31 bench,
    2026-07-27):

    * **HA's own onboarding must be finished.** If ``.storage/onboarding``
      still lists pending steps the frontend redirects every route to
      ``/onboarding.html`` and nothing renders. Seen on the K31 bench, which
      sits at ``{"done": ["user"]}`` — its pre-wipe backup shows the same, so
      that is how the bench was provisioned, not something a wipe did.
    * **The caller must be a master**, or the strategy never generates the
      view (that is the UI half of the gate, by design).
    * **The home must have at least one room with something in it.** With no
      devices assigned to an area the strategy short-circuits to its
      "no rooms" view and the Verwalten tab does not exist — correct
      behaviour, but nothing to test against.
    """
    r = await request_ctx.get("/api/onboarding")
    if r.ok:
        steps = await r.json()
        pending = [s["step"] for s in steps if not s.get("done")]
        if pending:
            pytest.skip(
                "Home Assistant's own onboarding is incomplete "
                f"(pending: {pending}) — the frontend redirects to "
                "/onboarding.html, so no dashboard renders. Point this at a "
                "tenant-complete canary."
            )

    r = await request_ctx.get(
        "/api/greenautarky_site/home_model",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.ok, await r.text()
    model = await r.json()
    if not model.get("is_master"):
        pytest.skip(
            f"{MASTER_USERNAME!r} is not a flagged master on this device — "
            "the strategy only generates the Verwalten view for masters"
        )
    if not (model.get("rooms") or []):
        pytest.skip(
            "the home has no rooms with devices in them, so the strategy "
            "renders its no-rooms view and there is no Verwalten tab — point "
            "this at a tenant-complete canary, not a freshly wiped bench"
        )


async def _open_master_dashboard(pw, token):
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(base_url=DEVICE_URL)
    hass_tokens = {
        "access_token": token, "token_type": "Bearer", "expires_in": 1800,
        "hassUrl": DEVICE_URL, "clientId": CLIENT_ID, "expires": 9999999999999,
    }
    await ctx.add_init_script(
        f"window.localStorage.setItem('hassTokens', {json.dumps(json.dumps(hass_tokens))});"
    )
    page = await ctx.new_page()
    card = page.locator("ga-master-card")
    # Over the mesh the frontend bundle can take a while, and the dashboard
    # only renders after the strategy has fetched the home model. One reload
    # absorbs the slow first paint; a second miss is a real failure, not load.
    for attempt in (1, 2):
        await page.goto("/lovelace/verwalten", wait_until="domcontentloaded")
        try:
            await card.wait_for(timeout=45000)
            return browser, page, card
        except playwright_async.TimeoutError:
            if attempt == 2:
                pytest.fail(
                    "ga-master-card never rendered on /lovelace/verwalten "
                    "after two attempts — either the bundle did not load or "
                    "this user is not a master (the strategy only generates "
                    "the Verwalten view for masters)"
                )
    raise AssertionError("unreachable")


@requires_device
async def test_danger_zone_renders_and_states_its_limits(socket_enabled) -> None:
    async with playwright_async.async_playwright() as pw:
        api = await pw.request.new_context(base_url=DEVICE_URL)
        browser = None
        try:
            token = await _token(api, MASTER_USERNAME, MASTER_PASSWORD)
            await _require_tenant_complete_device(api, token)
            browser, _page, card = await _open_master_dashboard(pw, token)

            await card.get_by_text("Gefahrenbereich").wait_for(timeout=10000)
            text = await card.inner_text()
            # Both entry points are present and say what they do. (The
            # promises about history and room names live in the DIALOGS —
            # a <dialog> yields no text until it is open, so they are
            # asserted in the two tests below that actually open them.)
            assert "Unter-Nutzer" in text
            assert "Nutzer zurücksetzen" in text
            assert "Alles löschen" in text
        finally:
            if browser:
                await browser.close()
            await api.dispose()


@requires_device
async def test_soft_reset_button_arms_only_on_the_exact_phrase(
    socket_enabled,
) -> None:
    async with playwright_async.async_playwright() as pw:
        api = await pw.request.new_context(base_url=DEVICE_URL)
        browser = None
        try:
            token = await _token(api, MASTER_USERNAME, MASTER_PASSWORD)
            await _require_tenant_complete_device(api, token)
            browser, _page, card = await _open_master_dashboard(pw, token)

            await card.locator("button.household-reset").click()
            confirm = card.locator("input.hh-confirm")
            go = card.locator("button.hh-go")
            await confirm.wait_for(timeout=10000)

            # The soft reset cannot remove recorder history — it is
            # entity-bound, not user-bound. If this copy ever goes, the button
            # is promising something it does not do.
            assert "Messwerte" in await card.locator("dialog.household-dlg").inner_text()

            assert await go.is_disabled(), "armed before anything was typed"
            await confirm.fill("loeschen")
            assert await go.is_disabled(), "armed on a near-miss phrase"
            await confirm.fill("LÖSCHEN")
            assert await go.is_enabled(), "correct phrase did not arm the button"

            # Never submit: cancel out.
            await card.locator("dialog.household-dlg button.dlg-cancel").click()
        finally:
            if browser:
                await browser.close()
            await api.dispose()


@requires_device
async def test_full_erase_needs_both_phrase_and_pin(socket_enabled) -> None:
    """Either input alone must leave the button dead — the server re-checks
    the PIN anyway, and the card mirrors it so the user meets a form, not a
    401."""
    async with playwright_async.async_playwright() as pw:
        api = await pw.request.new_context(base_url=DEVICE_URL)
        browser = None
        try:
            token = await _token(api, MASTER_USERNAME, MASTER_PASSWORD)
            await _require_tenant_complete_device(api, token)
            browser, _page, card = await _open_master_dashboard(pw, token)

            await card.locator("button.site-reset").click()
            pin = card.locator("input.site-pin")
            confirm = card.locator("input.site-confirm")
            go = card.locator("button.site-go")
            await pin.wait_for(timeout=10000)

            dialog_text = await card.locator("dialog.site-dlg").inner_text()
            assert "Umbenannte Räume" in dialog_text, (
                "the dialog must say rooms return to their default names"
            )

            assert await go.is_disabled()
            await confirm.fill("LÖSCHEN")
            assert await go.is_disabled(), "armed without a PIN"
            await confirm.fill("")
            await pin.fill("123456")
            assert await go.is_disabled(), "armed without the phrase"
            await pin.fill("12345")
            await confirm.fill("LÖSCHEN")
            assert await go.is_disabled(), "armed on a 5-digit PIN"
            await pin.fill("123-456")
            assert await go.is_enabled(), (
                "phrase + 6-digit PIN (dashes tolerated) did not arm the button"
            )

            # Never submit — this dialog ends in an irreversible wipe.
            await card.locator("dialog.site-dlg button.dlg-cancel").click()
        finally:
            if browser:
                await browser.close()
            await api.dispose()
