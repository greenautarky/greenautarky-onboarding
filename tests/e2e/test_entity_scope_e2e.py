"""E2E (Playwright) — Stage A entity scoping, proven in a REAL browser.

A curl proof (see tests/device) shows the API enforces the scope. This proves
the same holds inside an authenticated BROWSER session on the device: we log a
scoped sub-user in (token injected into HA's ``hassTokens`` localStorage, the
same shape the frontend writes), load the app, and from the page's own JS
(``page.evaluate`` → ``fetch``) assert that:

* ``/api/states`` returns only the scoped set (fewer than the master sees);
* reading a NON-scoped entity is 401;
* ``/api/greenautarky_site/my_rooms`` reports scope=rooms for him.

Self-cleaning; CANARIES ONLY.

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=... GA_DEVICE_MASTER_PASSWORD=... \
    pytest tests/e2e -m e2e
"""

from __future__ import annotations

import asyncio
import json
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
# entity_scoping is admin-only (CI master = plain tenant user). The testgate
# hands us the device admin credential; locally fall back to master creds.
ADMIN_USERNAME = os.environ.get("GA_DEVICE_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("GA_DEVICE_ADMIN_PASSWORD", "")

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / master credentials not set",
)

CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"
API = "/api/greenautarky_site"


async def _token(rc, username: str, password: str) -> str:
    r = await rc.post(
        "/auth/login_flow",
        data={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID},
    )
    assert r.ok, await r.text()
    flow = await r.json()
    r = await rc.post(
        f"/auth/login_flow/{flow['flow_id']}",
        data={"client_id": CLIENT_ID, "username": username, "password": password},
    )
    assert r.ok, await r.text()
    body = await r.json()
    assert "result" in body, f"login failed for {username!r}: {body.get('errors') or body}"
    code = body["result"]
    r = await rc.post(
        "/auth/token",
        form={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
    )
    assert r.ok, await r.text()
    return (await r.json())["access_token"]


@requires_device
async def test_scoped_subuser_in_the_browser() -> None:
    pw = playwright_async
    marker = uuid.uuid4().hex[:6]
    sub_name = f"E2EScope {marker}"
    sub_pw = f"pw-{secrets.token_urlsafe(12)}"

    async with pw.async_playwright() as p:
        api = await p.request.new_context(base_url=DEVICE_URL)
        master_tok = await _token(api, MASTER_USERNAME, MASTER_PASSWORD)
        mh = {"Authorization": f"Bearer {master_tok}"}
        if ADMIN_USERNAME and ADMIN_PASSWORD:
            ah = {"Authorization": f"Bearer {await _token(api, ADMIN_USERNAME, ADMIN_PASSWORD)}"}
        else:
            ah = mh

        pin = (await (await api.post(f"{API}/sub_user/invite", headers=mh, data={})).json())["pin"]

        sub_user_id = None
        scoping_was_on = False
        browser = None
        try:
            r = await api.post(
                f"{API}/sub_user/join",
                data={"invite_pin": pin, "name": sub_name, "password": sub_pw, "datenschutz_consent": True},
            )
            assert r.ok, await r.text()
            # join slugifies the display name — log in with the returned username
            sub_username = (await r.json())["username"]
            listing = await (await api.get(f"{API}/sub_user/list", headers=mh)).json()
            sub_user_id = next(s["user_id"] for s in listing["sub_users"] if s.get("name") == sub_name)

            scoping_was_on = (await (await api.get(f"{API}/entity_scoping", headers=ah)).json())["enabled"]
            r = await api.post(f"{API}/entity_scoping", headers=ah, data={"enabled": True})
            assert r.ok, await r.text()

            master_states = await (await api.get("/api/states", headers=mh)).json()
            victim = master_states[0]["entity_id"]

            sub_tok = await _token(api, sub_username, sub_pw)

            # authenticate the browser the way the HA frontend does: hassTokens
            browser = await p.chromium.launch()
            ctx = await browser.new_context(base_url=DEVICE_URL)
            hass_tokens = {
                "access_token": sub_tok,
                "token_type": "Bearer",
                "expires_in": 1800,
                "hassUrl": DEVICE_URL,
                "clientId": CLIENT_ID,
                "expires": 9999999999999,
            }
            await ctx.add_init_script(
                f"window.localStorage.setItem('hassTokens', {json.dumps(json.dumps(hass_tokens))});"
            )
            page = await ctx.new_page()
            await page.goto("/", wait_until="domcontentloaded")

            # from the PAGE's own JS: the scope is enforced on this session
            scoped = await page.evaluate(
                """async ({ t, victim, api }) => {
                    const h = { Authorization: 'Bearer ' + t };
                    const states = await (await fetch('/api/states', { headers: h })).json();
                    const victimResp = await fetch('/api/states/' + victim, { headers: h });
                    const rooms = await (await fetch(api + '/my_rooms', { headers: h })).json();
                    return { count: states.length, victimStatus: victimResp.status, scope: rooms.scope };
                }""",
                {"t": sub_tok, "victim": victim, "api": API},
            )

            assert scoped["count"] < len(master_states), (
                f"browser session not scoped: {scoped['count']} vs {len(master_states)}"
            )
            assert scoped["victimStatus"] == 401, f"non-scoped entity readable in browser: {scoped}"
            assert scoped["scope"] == "rooms", f"my_rooms scope was {scoped['scope']}"
        finally:
            await api.post(f"{API}/entity_scoping", headers=ah, data={"enabled": scoping_was_on})
            if sub_user_id:
                await api.post(f"{API}/sub_user/remove", headers=mh, data={"sub_user_id": sub_user_id})
            if browser:
                await browser.close()
            await api.dispose()


# ─── the dashboard must actually RENDER for a scoped user ─────────────────
#
# The test above proves the scope is enforced by fetching from inside the
# page. That says nothing about whether the page renders — and on rc36 it
# does not: the leak_guard's registry filtering raises, hass.entities /
# .devices / .areas stay null, and HA's own sidebar crashes indexing null.
# A scoped resident sees a blank screen. The API-level assertion was green
# the whole time.
#
# See greenautarky-onboarding#31 for the two-layer root cause.


@requires_device
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BROKEN as shipped in rc36 — a scoped user's dashboard does not "
        "render (registry WS commands fail, hass.entities/.devices/.areas are "
        "null, HA's sidebar crashes). Flips to a failure once fixed."
    ),
)
async def test_scoped_user_dashboard_actually_renders(socket_enabled) -> None:
    """A resident must SEE their rooms, not just be allowed to fetch them."""
    import json as _json

    marker = uuid.uuid4().hex[:6]
    sub_name = f"RenderTest {marker}"
    sub_pw = f"pw-{secrets.token_urlsafe(12)}"

    async with playwright_async.async_playwright() as p:
        api = await p.request.new_context(base_url=DEVICE_URL)
        browser = None
        sub_user_id = None
        try:
            mh = {"Authorization": f"Bearer {await _token(api, MASTER_USERNAME, MASTER_PASSWORD)}"}
            r = await api.post(f"{API}/sub_user/invite", headers=mh, data={})
            assert r.ok, await r.text()
            pin = (await r.json())["pin"]
            r = await api.post(
                f"{API}/sub_user/join",
                data={"invite_pin": pin, "name": sub_name, "password": sub_pw,
                      "datenschutz_consent": True},
            )
            assert r.ok, await r.text()
            sub_username = (await r.json())["username"]
            listing = await (await api.get(f"{API}/sub_user/list", headers=mh)).json()
            sub_user_id = next(s["user_id"] for s in listing["sub_users"]
                               if s.get("name") == sub_name)
            # give them a room, otherwise the strategy renders its no-rooms view
            areas = listing.get("areas") or []
            assert areas, "device has no areas to grant"
            await api.post(f"{API}/sub_user/assign_room", headers=mh,
                           data={"sub_user_id": sub_user_id,
                                 "area_id": areas[0]["area_id"], "assigned": True})

            sub_tok = await _token(api, sub_username, sub_pw)
            browser = await p.chromium.launch()
            ctx = await browser.new_context(base_url=DEVICE_URL)
            await ctx.add_init_script(
                "window.__rejections = [];"
                "window.addEventListener('unhandledrejection',"
                " e => window.__rejections.push(String(e.reason && e.reason.message || e.reason)));"
            )
            await ctx.add_init_script(
                "window.localStorage.setItem('hassTokens', "
                f"{_json.dumps(_json.dumps({'access_token': sub_tok, 'token_type': 'Bearer', 'expires_in': 1800, 'hassUrl': DEVICE_URL, 'clientId': CLIENT_ID, 'expires': 9999999999999}))});"
            )
            page = await ctx.new_page()
            await page.goto("/lovelace/0", wait_until="domcontentloaded")
            await asyncio.sleep(10)

            maps = await page.evaluate(
                "() => { const h = document.querySelector('home-assistant')?.hass;"
                " return h ? {entities: h.entities === null, devices: h.devices === null,"
                " areas: h.areas === null} : null; }"
            )
            rejections = await page.evaluate("() => window.__rejections || []")
            views = await page.locator("hui-view").count()

            assert maps and not any(maps.values()), (
                f"registry collections are null for a scoped user: {maps} — the "
                f"WS registry commands failed. Rejections: {rejections[:3]}"
            )
            assert views > 0, (
                f"the scoped user's dashboard did not render. Rejections: "
                f"{rejections[:3]}"
            )
        finally:
            if browser:
                await browser.close()
            if sub_user_id:
                await api.post(f"{API}/sub_user/remove", headers=mh,
                               data={"sub_user_id": sub_user_id})
            await api.dispose()
