"""E2E (local Core + Chromium) - the onboarding PIN never travels in a URL (Odoo #2159).

The device label's QR code exists so that a resident does not have to type the
PIN. 2.12.13 (F3c) switched the pre-fill off to keep the PIN out of URLs; 2.13.1
brings it back with the PIN in the URL FRAGMENT, which a browser never sends.

What is REAL here: Home Assistant Core with greenautarky_site installed from
source (``tests/e2e/local_core/launcher.py``), the committed setup bundle,
Chromium. In FRONT of Core sits a small logging reverse proxy that records what
a Caddy access log records: the request line (method + URI), the Referer, and
the Location of each answer. Assertions run on THAT record and on every request
URL Chromium itself reports.

    GA_E2E_HASS=/path/to/venv/bin/hass GA_E2E_BUNDLE=/path/to/ga-frontend-bundle \
        pytest tests/e2e/test_pin_not_in_url_e2e.py -m e2e

``GA_E2E_SITE`` points at another greenautarky_site package dir (the red run
against origin/main). Skipped without playwright / hass / bundle.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

playwright_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

pytestmark = [pytest.mark.e2e]

HASS = os.environ.get("GA_E2E_HASS", "")
BUNDLE = os.environ.get("GA_E2E_BUNDLE", "")
SITE = os.environ.get("GA_E2E_SITE", "")
PIN = "246813"
DEVICE = "KIB-SON-00000031"

needs_core = pytest.mark.skipif(
    not (HASS and BUNDLE), reason="GA_E2E_HASS / GA_E2E_BUNDLE not set"
)


class LoggingProxy:
    """Reverse proxy that keeps a Caddy-style access log."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.log: list[dict] = []
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        async with aiohttp.ClientSession(auto_decompress=False) as s:
            async with s.request(
                request.method, self.upstream + request.raw_path, headers=headers,
                data=body, allow_redirects=False,
            ) as up:
                payload = await up.read()
                out = web.Response(status=up.status, body=payload)
                for k, v in up.headers.items():
                    if k.lower() in ("transfer-encoding", "content-length", "connection"):
                        continue
                    out.headers.add(k, v)
                self.log.append({
                    "uri": f"{request.method} {request.raw_path}",
                    "referer": request.headers.get("Referer", ""),
                    "location": up.headers.get("Location", ""),
                    "status": up.status,
                })
                return out

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self._loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10)

    def stop(self) -> None:
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture(autouse=True)
def _allow_localhost():
    """pytest-homeassistant-custom-component blocks sockets; this tier talks to a
    Core and a proxy on 127.0.0.1 (tests/e2e/conftest.py only lifts it for a
    GA_DEVICE_URL)."""
    import pytest_socket

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    yield


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent / "local_core"))
    from launcher import LocalCore

    cfg = tmp_path_factory.mktemp("ga-e2e-core") / "core"
    port = int(os.environ.get("GA_E2E_PORT", "18124"))
    lc = LocalCore(HASS, Path(BUNDLE), cfg, port, Path(SITE) if SITE else None)
    return lc


def _provision_pin(lc) -> None:
    f = lc.config / ".storage" / "greenautarky_secrets" / "onboarding_pin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(PIN + "\n")
    # What provisioning stage 91 writes: the storage entry is the wizard opt-in.
    (lc.config / ".storage" / "greenautarky_site").write_text(json.dumps({
        "version": 2, "minor_version": 1, "key": "greenautarky_site",
        "data": {"completed": False, "tenant_mode": True, "gdpr_accepted": False,
                 "steps_done": [], "consents": {}},
    }))


def _wait_for_wizard_redirect(url: str, timeout: float = 90) -> None:
    """``/status`` answers before Home Assistant has finished starting; until the
    IndexView patch is live ``/`` serves HA's own frontend (which bounces to the
    login page). Wait for the state a device is in a minute after boot."""
    import time
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opener.open(url + "/", timeout=5)
        except urllib.error.HTTPError as err:
            if err.code == 302 and "greenautarky-setup" in err.headers.get("Location", ""):
                return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError("`/` never redirected to the wizard")


@pytest.fixture
def fresh_device(core):
    """A pending device: PIN file written, wizard not completed. One Core per
    test, because completing onboarding is one-way."""
    core.prepare()
    _provision_pin(core)
    proxy = LoggingProxy(core.url)
    try:
        core.start()
        _wait_for_wizard_redirect(core.url)
        proxy.start()
        yield core, proxy
    finally:
        proxy.stop()
        core.stop()


def _status(page) -> dict:
    return page.evaluate("fetch('/api/greenautarky_site/status').then(r => r.json())")


def _walk_the_wizard(page) -> None:
    """Start -> (PIN is pre-filled and submitted by the page) -> finish the rest
    through the same endpoints the panel calls, from the same browser session."""
    page.locator("ga-setup-welcome").wait_for(timeout=30_000)
    assert "/greenautarky-setup.html" in page.url, page.url
    # The welcome step has one primary button; the page then moves to the PIN
    # step, where an auto-filled PIN is submitted without a keystroke.
    page.locator("ga-setup-welcome").get_by_role("button").first.click(timeout=30_000)
    for _ in range(40):
        if _status(page)["pin_verified"] is True:
            break
        page.wait_for_timeout(500)
    else:
        raise AssertionError(
            "the PIN was NOT pre-filled/submitted from the QR URL: the wizard is still "
            f"waiting at the PIN step ({page.url!r})"
        )
    js = """
    async () => {
      const post = (p, b) => fetch('/api/greenautarky_site/' + p, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
      const out = {};
      out.gdpr = (await post('gdpr', {accepted: true})).status;
      out.user = (await post('create_user', {
        client_id: location.origin + '/', name: 'E2E Resident',
        username: 'resident@example.invalid', password: 'e2e-Resident-pw-1'})).status;
      out.telemetry = (await post('telemetry', {error_logs: true, metrics: false})).status;
      out.complete = (await post('complete', {})).status;
      return out;
    }
    """
    steps = page.evaluate(js)
    assert steps == {"gdpr": 200, "user": 200, "telemetry": 200, "complete": 200}, steps
    assert _status(page)["completed"] is True


def _open_and_finish(core, proxy, entry_path: str):
    seen_urls: list[str] = []
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_context(locale="de-DE").new_page()
            page.on("request", lambda r: seen_urls.append(r.url))
            page.goto(proxy.url + entry_path)
            _walk_the_wizard(page)
            final_url = page.url
        finally:
            browser.close()
    return seen_urls, final_url


@needs_core
def test_fragment_qr_prefills_the_pin_and_no_url_carries_it(fresh_device):
    core, proxy = fresh_device
    seen, final_url = _open_and_finish(core, proxy, f"/#pin={PIN}&device={DEVICE}")

    # onboarding finished (asserted inside the walk) - and nothing leaked:
    assert not [e for e in proxy.log if PIN in str(e)], (
        "the PIN reached the access log: "
        + repr([e for e in proxy.log if PIN in str(e)])
    )
    # Chromium reports requests WITHOUT the fragment; the PIN must not be in any.
    assert not [u for u in seen if PIN in u], [u for u in seen if PIN in u]
    assert PIN not in final_url, "the page left the PIN in the address bar"
    assert len(proxy.log) > 5, "the proxy saw almost nothing - the test would be vacuous"


@needs_core
def test_legacy_query_qr_still_prefills_but_is_not_echoed(fresh_device):
    """A label printed before 2.13.1. The FIRST request line necessarily carries
    the PIN (the printed URL; the Caddy log filter's job). Everything after it -
    Location, Referer, later requests, the address bar - must not."""
    core, proxy = fresh_device
    seen, final_url = _open_and_finish(core, proxy, f"/?pin={PIN}&device={DEVICE}")

    first, rest = proxy.log[0], proxy.log[1:]
    assert PIN in first["uri"], "test setup: the legacy request line should carry the PIN"
    assert not [e for e in rest if PIN in str(e)], [e for e in rest if PIN in str(e)]
    assert PIN not in first["location"], "the redirect echoed the PIN in Location"
    assert not [u for u in seen[1:] if PIN in u], [u for u in seen[1:] if PIN in u]
    assert PIN not in final_url
