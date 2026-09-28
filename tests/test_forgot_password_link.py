"""The login page's "Forgot password?" leads to our PIN reset (ADR-0040).

THE DEFECT: Home Assistant's login page is stock, and its "Forgot password?"
link goes to home-assistant.io/docs/locked_out — container command lines. A
resident who forgot the password landed there (reported 2026-09-28), while this
component has shipped a PIN-gated reset page that nothing linked to.

These tests drive the route exactly the way Core registers it: the `frontend`
component calls ``hass.http.async_register_static_paths`` with a
``StaticPathConfig("/auth/authorize", <authorize.html>, False)``. Registering a
look-alike route by hand would test our idea of Core, not Core — so the seam
under test is Core's own registration code. Only the HTML file is ours, because
the test venv has no ``hass_frontend`` package.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from homeassistant.components.http import StaticPathConfig
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import get_test_config_dir

from greenautarky_site.const import PIN_FILE
from greenautarky_site.onboarding.forgot_password_link import (
    AUTHORIZE_PATH,
    FORGOT_PASSWORD_SCRIPT,
    HELP_URL,
    MARKER,
    RESET_PAGE_URL,
    async_patch_authorize_page,
    inject_forgot_password_script,
)

# The shape that matters: HA's page ends in </body></html> and carries the
# element that renders the stock link.
STOCK_PAGE = (
    "<!DOCTYPE html><html><head><title>Home Assistant</title></head>"
    "<body><div class='content'><ha-authorize></ha-authorize></div></body></html>"
)


def _write_pin() -> None:
    path = Path(get_test_config_dir()) / PIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("123456")


@pytest.fixture
async def authorize_route(hass, tmp_path):
    """/auth/authorize registered by Core's own static-path code."""
    await async_setup_component(hass, "http", {})
    page = tmp_path / "authorize.html"
    page.write_text(STOCK_PAGE)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(AUTHORIZE_PATH, str(page), False)]
    )
    return page


async def test_login_page_carries_the_retarget_script(
    hass, authorize_route, hass_client_no_auth
):
    """The resident's path: device with a PIN, login page, the link is ours."""
    _write_pin()
    assert await async_patch_authorize_page(hass) is True

    client = await hass_client_no_auth()
    resp = await client.get(AUTHORIZE_PATH)
    body = await resp.text()

    assert resp.status == 200
    assert f'var TARGET = "{RESET_PAGE_URL}"' in body, (
        "the login page does not carry the retarget script — 'Forgot password?' "
        "still sends the resident to home-assistant.io"
    )
    assert "<ha-authorize></ha-authorize>" in body, "the stock page must survive"
    assert body.index(MARKER) < body.index("</body>")
    assert resp.content_type == "text/html"


async def test_a_gz_sibling_of_the_login_page_cannot_shadow_the_injection(
    hass, authorize_route, hass_client_no_auth
):
    """2.11.0 ships precompressed ``.gz`` siblings, and aiohttp's FileResponse
    sends ``<file>.gz`` INSTEAD of the file whenever one exists and the browser
    accepts gzip. A page modified at serve time must therefore never go out
    through FileResponse: Core's frontend may ship ``authorize.html.gz`` (the
    stock bytes), and every real browser sends ``Accept-Encoding: gzip`` — the
    resident would get the stock page, link to home-assistant.io and all,
    while every test without that header stayed green."""
    import gzip

    authorize_route.with_name("authorize.html.gz").write_bytes(
        gzip.compress(STOCK_PAGE.encode())
    )
    _write_pin()
    assert await async_patch_authorize_page(hass) is True

    client = await hass_client_no_auth()
    resp = await client.get(AUTHORIZE_PATH, headers={"Accept-Encoding": "gzip"})
    body = await resp.text()

    assert resp.status == 200
    assert MARKER in body, (
        "a gzip-accepting browser got the stock login page from its .gz sibling "
        "— 'Forgot password?' still leads to home-assistant.io"
    )


async def test_the_patch_reaches_a_route_that_already_served_requests(
    hass, authorize_route, hass_client_no_auth
):
    """Same trap as the IndexView redirect: on a device the login page is hit
    (browser, probes) before a custom component finishes setup, and the router
    is frozen once the server runs. The patch must still reach the live route."""
    _write_pin()
    client = await hass_client_no_auth()
    first = await (await client.get(AUTHORIZE_PATH)).text()
    assert MARKER not in first  # precondition: served stock before the patch

    assert await async_patch_authorize_page(hass) is True

    second = await (await client.get(AUTHORIZE_PATH)).text()
    assert MARKER in second, (
        "a route that had already served a request kept its old handler — the "
        "patch never reached the running server"
    )


async def test_no_pin_file_keeps_home_assistants_page(
    hass, authorize_route, hass_client_no_auth
):
    """Green in the other direction: without a PIN the reset page answers 404
    'No PIN configured', so pointing the link there would be worse than the
    stock one. The page must be byte-identical to Core's."""
    assert await async_patch_authorize_page(hass) is True

    client = await hass_client_no_auth()
    body = await (await client.get(AUTHORIZE_PATH)).text()

    assert body == STOCK_PAGE


async def test_patching_twice_injects_once(hass, authorize_route, hass_client_no_auth):
    """An integration reload runs setup again; the page must not grow a second
    script (two click handlers would still work — a growing page would not)."""
    _write_pin()
    assert await async_patch_authorize_page(hass) is True
    assert await async_patch_authorize_page(hass) is True

    client = await hass_client_no_auth()
    body = await (await client.get(AUTHORIZE_PATH)).text()
    assert body.count(MARKER) == 1


async def test_missing_route_is_loud_and_harmless(hass, caplog):
    """No /auth/authorize (frontend not loaded): setup must go on, and the log
    must say WHICH link is still wrong — not a silent no-op (rule 44)."""
    await async_setup_component(hass, "http", {})
    with caplog.at_level(logging.WARNING):
        assert await async_patch_authorize_page(hass) is False
    assert "home-assistant.io" in caplog.text
    assert RESET_PAGE_URL in caplog.text


async def test_an_unknown_handler_is_left_alone(hass, caplog):
    """If Core ever serves the page differently, we must not guess a file: the
    stock handler stays, and the log names what we found."""
    await async_setup_component(hass, "http", {})

    async def _core_changed(request):  # pragma: no cover - never called
        raise AssertionError

    hass.http.app.router.add_get(AUTHORIZE_PATH, _core_changed)
    with caplog.at_level(logging.WARNING):
        assert await async_patch_authorize_page(hass) is False
    assert "_core_changed" in caplog.text


def test_script_lands_before_body_end_and_only_once():
    once = inject_forgot_password_script(STOCK_PAGE)
    assert once.count(MARKER) == 1
    assert once.index(FORGOT_PASSWORD_SCRIPT) < once.index("</body>")
    assert inject_forgot_password_script(once) == once
    # No </body> at all: append instead of dropping the script.
    assert inject_forgot_password_script("<p>x</p>").endswith(FORGOT_PASSWORD_SCRIPT)


def test_the_help_button_is_removed_until_a_ga_help_page_is_live():
    """ADR-0040 D7, pinned: the shipped HELP_URL is empty (= remove Home
    Assistant's Help button). Switch it on only in the change that makes the
    help page live — a link to a domain that does not answer is worse than no
    button. The browser tier proves what each value does."""
    assert HELP_URL == ""
    assert 'var HELP_TARGET = "";' in FORGOT_PASSWORD_SCRIPT


async def test_integration_setup_installs_the_patch(
    hass, authorize_route, hass_client_no_auth
):
    """Reachability: the function above is worth nothing if setup never calls
    it. Run the real async_setup (with the same four frontend helpers patched
    out that test_setup.py patches) and read the page a browser would get."""
    from unittest.mock import patch

    from greenautarky_site import async_setup
    from greenautarky_site.const import DOMAIN

    _write_pin()
    with (
        patch("greenautarky_site._async_register_frontend_bundle", return_value=None),
        patch("greenautarky_site._async_register_panel", return_value=None),
        patch("greenautarky_site._register_redirect_js", return_value=None),
        patch(
            "greenautarky_site._patch_index_view_for_wizard_redirect",
            return_value=None,
        ),
    ):
        assert await async_setup(hass, {DOMAIN: {}}) is True
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    body = await (await client.get(AUTHORIZE_PATH)).text()
    assert MARKER in body, "async_setup never patched the login page"
