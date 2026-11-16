r"""E2E (Playwright) — the resident's Einstellungen page against a real Core.
Odoo #2144 (Zigbee list), #2145 (software tile), #2146 (no search / Assist).

Runs against ANY real Core: a canary, or the throwaway one that
``tests/e2e/local_core/launcher.py`` starts (no device needed — a canary may be
busy with a hand test, and a release gate must not depend on one).

    GA_DEVICE_URL=http://127.0.0.1:18123 \
    GA_DEVICE_MASTER_USERNAME=... GA_DEVICE_MASTER_PASSWORD=... \   # a resident (non-admin master)
    GA_DEVICE_ADMIN_USERNAME=... GA_DEVICE_ADMIN_PASSWORD=... \     # the service view
    [GA_E2E_SEEDED=1 GA_E2E_STATUS_FILE=...] [GA_E2E_SHOTS=/dir]    # launcher output
    pytest tests/e2e -m e2e -k resident_settings

Two layers of assertion:

* ALWAYS: what the card shows equals what the server's own endpoints return for
  the same user (cards rendered from the registry / the version source, not from
  a copy), and search + Assist are absent for the resident and present for admin.
* With ``GA_E2E_SEEDED=1`` (the launcher's flat): the pinned expectations below —
  rooms, battery, LQI, "ohne Raum", the release headline, and a changed version
  shown as it is after a reload.

Read-only: nothing here writes to Home Assistant.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

playwright_async = pytest.importorskip(
    "playwright.async_api", reason="playwright not installed"
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

from device_target import CLIENT_ID, DEVICE_URL  # noqa: E402

MASTER = (os.environ.get("GA_DEVICE_MASTER_USERNAME", ""),
          os.environ.get("GA_DEVICE_MASTER_PASSWORD", ""))
ADMIN = (os.environ.get("GA_DEVICE_ADMIN_USERNAME", ""),
         os.environ.get("GA_DEVICE_ADMIN_PASSWORD", ""))
SEEDED = os.environ.get("GA_E2E_SEEDED") == "1"
STATUS_FILE = os.environ.get("GA_E2E_STATUS_FILE", "")
SHOTS = os.environ.get("GA_E2E_SHOTS", "")

requires_users = pytest.mark.skipif(
    not (DEVICE_URL and all(MASTER) and all(ADMIN)),
    reason="GA_DEVICE_URL / master + admin credentials not set",
)

# Pinned, never derived from the code under test (the launcher's seed flat).
SEEDED_ROWS = [
    ("Thermostat Küche", "Küche", "9\u00a0% niedrig", "21 schwach"),
    ("Thermostat Wohnzimmer", "Wohnzimmer", "88\u00a0%", "120"),
    ("Steckdose Flur", "ohne Raum", "\u2013", "200"),
]
SEEDED_RELEASE = "BOSv1.6.1-rc2"

# The two icons of the dashboard's top bar (@mdi/js mdiMagnify,
# mdiCommentProcessingOutline) as numbers+commands, to find the buttons.
_TOPBAR_BUTTONS = """
() => {
  const SEARCH = "M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z";
  const ASSIST = "M9,22A1,1 0 0,1 8,21V18H4A2,2 0 0,1 2,16V4C2,2.89 2.9,2 4,2H20A2,2 0 0,1 22,4V16A2,2 0 0,1 20,18H13.9L10.2,21.71C10,21.9 9.75,22 9.5,22V22H9M10,16V19.08L13.08,16H20V4H4V16H10M17,11H15V9H17V11M13,11H11V9H13V11M9,11H7V9H9V11Z";
  const found = { toolbar: false, search: 0, assist: 0 };
  const visible = (el) => {
    for (let n = el; n; n = n.parentNode || n.host) {
      if (n.nodeType === 1) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || n.hidden) return false;
      }
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.localName === 'hui-root') found.toolbar = true;
      if (el.path === SEARCH && visible(el)) found.search++;
      if (el.path === ASSIST && visible(el)) found.assist++;
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return found;
}
"""

_OPEN_DIALOGS = """
() => {
  const names = new Set();
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (['ha-voice-command-dialog', 'ha-quick-bar'].includes(el.localName)) names.add(el.localName);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return [...names];
}
"""


async def _login(pw, username: str, password: str):
    api = await pw.request.new_context(base_url=DEVICE_URL)
    try:
        r = await api.post("/auth/login_flow", data={
            "client_id": CLIENT_ID, "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID})
        flow = await r.json()
        r = await api.post(f"/auth/login_flow/{flow['flow_id']}", data={
            "client_id": CLIENT_ID, "username": username, "password": password})
        code = (await r.json()).get("result")
        assert code, "login flow did not finish"
        r = await api.post("/auth/token", form={
            "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
        return (await r.json())["access_token"]
    finally:
        await api.dispose()


async def _page(pw, token: str):
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(base_url=DEVICE_URL,
                                    viewport={"width": 1280, "height": 1400})
    hass_tokens = {"access_token": token, "token_type": "Bearer", "expires_in": 1800,
                   "hassUrl": DEVICE_URL, "clientId": CLIENT_ID, "expires": 9999999999999}
    await ctx.add_init_script(
        f"window.localStorage.setItem('hassTokens', {json.dumps(json.dumps(hass_tokens))});")
    return browser, await ctx.new_page()


async def _api(pw, token: str, path: str) -> dict:
    api = await pw.request.new_context(
        base_url=DEVICE_URL, extra_http_headers={"Authorization": f"Bearer {token}"})
    try:
        r = await api.get(path)
        assert r.ok, f"{path}: {r.status} {await r.text()}"
        return await r.json()
    finally:
        await api.dispose()


async def _settings(pw, token: str):
    browser, page = await _page(pw, token)
    await page.goto("/lovelace/einstellungen", wait_until="domcontentloaded")
    try:
        await page.locator("ga-master-card").wait_for(timeout=60000)
        await page.locator(".sw-bundle").wait_for(timeout=30000)
    except playwright_async.TimeoutError:
        await browser.close()
        pytest.fail("ga-master-card (with its software tile) never rendered on "
                    "/lovelace/einstellungen")
    return browser, page


async def _shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


async def _rows(page):
    """The Zigbee rows as shown. Locators, not document.querySelector: the card
    lives inside Home Assistant's shadow roots, which only locators pierce."""
    await page.locator(".zb-row").first.wait_for(timeout=30000)
    out = []
    for row in await page.locator(".zb-row").all():
        out.append([
            await row.locator(".zb-name").text_content(),
            await row.locator(".zb-room").text_content(),
            await row.locator(".zb-batt").text_content(),
            await row.locator(".zb-lqi").text_content(),
        ])
    return out


async def _shown_versions(page) -> dict:
    out = {}
    for row in await page.locator(".sw-row").all():
        out[await row.get_attribute("data-id")] = await row.locator(".sw-ver").text_content()
    return out


@requires_users
async def test_zigbee_card_renders_the_registry_before_the_danger_zone(socket_enabled) -> None:
    async with playwright_async.async_playwright() as pw:
        token = await _login(pw, *MASTER)
        browser, page = await _settings(pw, token)
        try:
            rows = await _rows(page)
            api = (await _api(pw, token, "/api/greenautarky_site/zigbee_devices"))["devices"]
            # The card is the server's list, in the server's (room) order.
            assert [r[0] for r in rows] == [d["name"] for d in api]
            assert [r[1] == "ohne Raum" for r in rows] == [d["area"] is None for d in api]

            heads = await page.locator("ga-master-card h4").all_text_contents()
            order = [next((i for i, t in enumerate(heads) if key in t), -1)
                     for key in ("Zigbee", "Software", "Gefahrenbereich")]
            assert -1 < order[0] < order[1] < order[2], order
            if SEEDED:
                assert [tuple(r) for r in rows] == SEEDED_ROWS
                assert await page.locator(".zb-batt .bad").count() == 1  # only the 9 % one
                assert await page.locator(".zb-lqi .bad").count() == 1  # only the 21 one
            await _shot(page, "einstellungen-resident")
        finally:
            await browser.close()


@requires_users
async def test_version_tile_shows_the_bundle_and_every_sub_version(socket_enabled) -> None:
    async with playwright_async.async_playwright() as pw:
        token = await _login(pw, *MASTER)
        browser, page = await _settings(pw, token)
        try:
            api = await _api(pw, token, "/api/greenautarky_site/versions")
            headline = await page.locator(".sw-bundle").inner_text()
            assert headline == (api["bundle"] or headline)
            await page.locator(".sw-tile summary").click()
            shown = await _shown_versions(page)
            # Every version the server reports is on screen, as reported.
            assert shown == {c["id"]: c["version"] for c in api["components"]}
            if SEEDED:
                assert headline == SEEDED_RELEASE
                assert shown["core"] == "2026.10.4"
                assert shown["99f1cad4_ga_manager"] == "0.277.6"
                assert shown["45df7312_zigbee2mqtt"] == "2.12.1"
                assert shown["ga_frontend_bundle"], "the integration versions come from Core"
                assert shown["greenautarky_site"], "the integration versions come from Core"
            else:
                assert re.fullmatch(r"BOSv\d+\.\d+\.\d+(-(rc|dev)\d+)?", headline), headline
            await _shot(page, "einstellungen-resident-versions-open")
        finally:
            await browser.close()


@pytest.mark.skipif(not (SEEDED and STATUS_FILE), reason="needs the local-Core launcher")
@requires_users
async def test_a_changed_version_is_shown_as_it_is_after_a_reload(socket_enabled) -> None:
    """THE RED ONE of #2145: no stale value."""
    path = Path(STATUS_FILE)
    original = path.read_text()
    async with playwright_async.async_playwright() as pw:
        token = await _login(pw, *MASTER)
        browser, page = await _settings(pw, token)
        try:
            assert await page.locator(".sw-bundle").inner_text() == SEEDED_RELEASE
            changed = json.loads(original)
            changed["ga_release"] = "BOSv1.6.1-rc3"
            for chk in changed["checks"]:
                if chk["name"] == "versions.collect":
                    chk["details"]["core"]["version"] = "2026.10.5"
            path.write_text(json.dumps(changed))
            await page.reload(wait_until="domcontentloaded")
            await page.locator(".sw-bundle").wait_for(timeout=30000)
            assert await page.locator(".sw-bundle").inner_text() == "BOSv1.6.1-rc3"
            await page.locator(".sw-tile summary").click()
            core = page.locator('.sw-row[data-id="core"] .sw-ver')
            assert await core.inner_text() == "2026.10.5"
        finally:
            path.write_text(original)
            await browser.close()


async def _dashboard(pw, creds):
    token = await _login(pw, *creds)
    browser, page = await _page(pw, token)
    await page.goto("/lovelace/0", wait_until="domcontentloaded")
    await page.wait_for_function(
        "() => !!document.querySelector('home-assistant') && "
        "!!document.querySelector('home-assistant').hass && "
        "!!document.querySelector('home-assistant').hass.user", timeout=60000)
    # The top bar: wait until the dashboard element is up, then let the
    # asset's poll (1 s) and any re-render settle.
    for _ in range(60):
        if (await page.evaluate(_TOPBAR_BUTTONS))["toolbar"]:
            break
        await page.wait_for_timeout(1000)
    await page.wait_for_timeout(3500)
    return browser, page


async def _press_and_list(page, key: str) -> list[str]:
    await page.evaluate("() => document.activeElement && document.activeElement.blur()")
    await page.locator("body").click(position={"x": 5, "y": 5}, force=True)
    await page.keyboard.press(key)
    await page.wait_for_timeout(1500)
    return await page.evaluate(_OPEN_DIALOGS)


@requires_users
async def test_search_and_assist_are_absent_for_the_resident(socket_enabled) -> None:
    async with playwright_async.async_playwright() as pw:
        browser, page = await _dashboard(pw, MASTER)
        try:
            assert (await page.evaluate("() => document.querySelector('home-assistant')"
                                        ".hass.user.is_admin")) is False, "not a resident"
            found = await page.evaluate(_TOPBAR_BUTTONS)
            assert found["toolbar"], "the dashboard top bar never rendered"
            assert (found["search"], found["assist"]) == (0, 0), found
            for key in ("a", "e", "c", "d", "Control+k"):
                assert await _press_and_list(page, key) == [], f"{key} opened something"
            await _shot(page, "dashboard-resident-no-search-no-assist")
        finally:
            await browser.close()


@requires_users
async def test_search_and_assist_are_absent_on_the_einstellungen_page_too(socket_enabled) -> None:
    """The page the ticket names (/lovelace/einstellungen), not just the overview."""
    async with playwright_async.async_playwright() as pw:
        token = await _login(pw, *MASTER)
        browser, page = await _settings(pw, token)
        try:
            await page.wait_for_timeout(2500)  # the asset's poll + any re-render
            found = await page.evaluate(_TOPBAR_BUTTONS)
            assert found["toolbar"], "the dashboard top bar never rendered"
            assert (found["search"], found["assist"]) == (0, 0), found
            await _shot(page, "einstellungen-resident-no-search-no-assist")
        finally:
            await browser.close()


@requires_users
async def test_search_and_assist_are_present_for_the_admin(socket_enabled) -> None:
    async with playwright_async.async_playwright() as pw:
        browser, page = await _dashboard(pw, ADMIN)
        try:
            assert (await page.evaluate("() => document.querySelector('home-assistant')"
                                        ".hass.user.is_admin")) is True
            found = await page.evaluate(_TOPBAR_BUTTONS)
            assert found["toolbar"], "the dashboard top bar never rendered"
            assert found["search"] >= 1, f"admin lost the search button: {found}"
            assert found["assist"] >= 1, f"admin lost the Assist button: {found}"
            assert "ha-voice-command-dialog" in await _press_and_list(page, "a")
            await _shot(page, "dashboard-admin-search-and-assist")
        finally:
            await browser.close()
