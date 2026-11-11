"""The `/admin` bypass, end to end, against the integration's REAL views.

Odoo #1888: `/admin` set the `ga_bypass=1` cookie and the status view honoured
it, but the IndexView redirect patch ignored it, so an admin returning from the
HA login (`/config?code=...`) was bounced to the wizard. Unit tests on the
patch alone cannot see that class: the chain spans GAAdminBypassView, HA's
auth route, the patched IndexView and the status view. These tests register
the real views in a real HA http stack (nothing about them is mocked) and walk
the chain with a cookie-carrying aiohttp client.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from homeassistant.components.frontend import DATA_PANELS, IndexView
from homeassistant.setup import async_setup_component

from greenautarky_site import _patch_index_view_for_wizard_redirect
from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding.wizard import (
    GAAdminBypassView,
    GAOnboardingStatusView,
)

SETUP_PATH = "/greenautarky-setup.html"
STATUS = "/api/greenautarky_site/status"


@pytest.fixture
def restore_index_view():
    """The patch marks the CLASS; undo it so tests do not leak into each other."""
    original_get = IndexView.get
    had_flag = getattr(IndexView, "_ga_wizard_patched", False)
    yield
    IndexView.get = original_get  # type: ignore[method-assign]
    if not had_flag and hasattr(IndexView, "_ga_wizard_patched"):
        delattr(IndexView, "_ga_wizard_patched")


async def _pending_instance(hass, hass_client_no_auth):
    """Real http stack, the integration's REAL views, real IndexView + patch.

    `hass_frontend` (the compiled UI) is not installed in the test env, so the
    `frontend` component cannot be set up. What we need from it is the IndexView
    ROUTING, which is real; only Core's own `get` body (renders index.html) is
    replaced by a 200 stub. The integration's patch, views and the status
    handler are the production code. The route is probed BEFORE the patch so
    the cached-`_route` race (903414f) is reproduced.
    """
    assert await async_setup_component(hass, "http", {"http": {}})
    hass.data[DATA_PANELS] = {"config": None, "lovelace": None}

    async def _core_get(self, request):
        return web.Response(status=200, text="home assistant")

    IndexView.get = _core_get  # type: ignore[method-assign]
    hass.http.app.router.register_resource(IndexView(None, hass))
    hass.data[DOMAIN] = {"state": {"completed": False}}
    hass.http.register_view(GAAdminBypassView())
    hass.http.register_view(GAOnboardingStatusView())
    client = await hass_client_no_auth()
    # Probe: materialises IndexView._route with the ORIGINAL handler.
    probe = await client.get("/", allow_redirects=False)
    assert probe.status == 200
    _patch_index_view_for_wizard_redirect(hass)
    return client


async def test_admin_bypass_chain_end_to_end(
    hass, hass_client_no_auth, restore_index_view
):
    client = await _pending_instance(hass, hass_client_no_auth)

    # 1. /admin -> /auth/authorize with the OAuth params + the bypass cookie.
    resp = await client.get("/admin", allow_redirects=False)
    assert resp.status == 302
    target = urlsplit(resp.headers["location"])
    assert target.path == "/auth/authorize"
    q = {k: v[0] for k, v in parse_qs(target.query).items()}
    origin = f"http://{resp.url.host}:{resp.url.port}"
    assert q["client_id"] == f"{origin}/"
    assert q["redirect_uri"] == f"{origin}/config?auth_callback=1"
    cookie = resp.cookies["ga_bypass"]
    assert cookie.value == "1"
    assert cookie["httponly"]
    # The jar would replay the cookie on every later call; send it explicitly
    # instead so "with" and "without" are unambiguous.
    client.session.cookie_jar.clear()

    # 2. OAuth return with the cookie: the frontend shell, NOT the wizard.
    redirect_path = urlsplit(q["redirect_uri"])
    land = await client.get(
        f"{redirect_path.path}?{redirect_path.query}&code=abc",
        cookies={"ga_bypass": "1"},
        allow_redirects=False,
    )
    assert land.status != 302 or SETUP_PATH not in land.headers.get("location", ""), (
        "admin bounced to the wizard after login"
    )
    assert land.status == 200, land.status

    # 3. Status view with the cookie reports completed=true ...
    st = await client.get(STATUS, cookies={"ga_bypass": "1"})
    assert (await st.json())["completed"] is True
    # ... and without it the device is still pending.
    st = await client.get(STATUS)
    assert (await st.json())["completed"] is False

    # 4. Same frontend route without the cookie -> wizard.
    nocookie = await client.get("/config", allow_redirects=False)
    assert nocookie.status == 302
    assert nocookie.headers["location"].startswith(SETUP_PATH)


async def test_patch_is_effective_and_honours_the_cookie_in_one_handler(
    hass, hass_client_no_auth, restore_index_view
):
    """Guard for the 903414f class, both halves in ONE test.

    #43 made the redirect always fire by dropping the cached `_route`; the
    bypass had silently depended on it NOT firing. Half A proves the patch is
    effective (no cookie + pending -> redirect, so the cached route was
    replaced); half B proves that same patched handler honours the cookie.
    Weakening either half (no patch, or a patch that ignores the cookie)
    fails here.
    """
    client = await _pending_instance(hass, hass_client_no_auth)

    routes = [r for r in hass.http.app.router.resources() if isinstance(r, IndexView)]
    assert routes, "no IndexView resource registered"
    assert getattr(IndexView, "_ga_wizard_patched", False)
    handler = routes[0]._route.handler
    assert getattr(handler, "__wrapped__", None) is not None, (
        "the live IndexView route does not hold the patched handler"
    )

    # A: effective.
    no_cookie = await client.get("/", allow_redirects=False)
    assert no_cookie.status == 302
    assert no_cookie.headers["location"].startswith(SETUP_PATH)
    # B: same handler, cookie honoured.
    with_cookie = await client.get(
        "/", cookies={"ga_bypass": "1"}, allow_redirects=False
    )
    assert with_cookie.status == 200


def test_admin_view_docstring_names_the_real_consumer():
    doc = GAAdminBypassView.__doc__ or ""
    assert "_patch_index_view_for_wizard_redirect" in doc
