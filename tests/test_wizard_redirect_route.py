"""The `/` → wizard redirect must survive a route that was already cached.

Regression test for a defect seen in the field: stored state said
``completed: false``, ``/greenautarky-setup.html`` served the wizard, and ``/``
still answered 200 with Home Assistant's own frontend. The QR code on the
device label points at ``/``, so a customer scanning it landed in Home
Assistant instead of the setup wizard — silently, with nothing in the log.

Mechanism: ``IndexView._route`` is a ``cached_property`` returning
``ResourceRoute("GET", self.get, self)``. It binds ``self.get`` at FIRST
ACCESS and caches the finished route on the instance. ``resolve()`` touches it
on every request that reaches the resource, so any hit before our
custom_component finishes setting up freezes the ORIGINAL handler into the
route. Re-assigning ``IndexView.get`` on the class afterwards is then a no-op.

These tests drive the REAL ``homeassistant.components.frontend.IndexView`` —
stubbing it would test our idea of Core rather than Core.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.frontend import IndexView

from greenautarky_site import _patch_index_view_for_wizard_redirect
from greenautarky_site.const import DOMAIN

SETUP_PATH = "/greenautarky-setup.html"


@pytest.fixture
def unpatched_index_view():
    """Undo the class-level monkey-patch around each test.

    The patch marks the CLASS, so without this a test that runs second sees
    ``_ga_wizard_patched`` already set, returns early, and passes for the
    wrong reason.
    """
    original_get = IndexView.get
    had_flag = getattr(IndexView, "_ga_wizard_patched", False)
    yield
    IndexView.get = original_get  # type: ignore[method-assign]
    if not had_flag and hasattr(IndexView, "_ga_wizard_patched"):
        delattr(IndexView, "_ga_wizard_patched")


def _fake_hass(view: IndexView, *, completed: bool) -> SimpleNamespace:
    """Minimal hass exposing what the patch reads: state + router resources."""
    return SimpleNamespace(
        data={DOMAIN: {"state": {"completed": completed}}},
        http=SimpleNamespace(
            app=SimpleNamespace(
                router=SimpleNamespace(resources=lambda: [view]),
            )
        ),
    )


async def _handler_response(view: IndexView):
    handler = view._route.handler
    return handler, await handler(SimpleNamespace())


async def test_redirect_fires_when_the_route_was_cached_before_the_patch(
    unpatched_index_view,
):
    """The failure seen in the field: an early request froze the old handler."""
    view = IndexView(None, None)  # type: ignore[arg-type]
    hass = _fake_hass(view, completed=False)
    view.hass = hass  # type: ignore[assignment]

    # An earlier request — monitoring probe, fleet-manager poll, a browser —
    # resolves through the resource and materialises the cached route.
    view._route  # noqa: B018  - accessing it IS the precondition

    _patch_index_view_for_wizard_redirect(hass)

    # Identity FIRST: on the buggy path the cached route still holds Core's
    # original handler, and calling it raises on our minimal request — an
    # unrelated AttributeError that hides the actual defect. Assert the thing
    # that is wrong, then assert the behaviour.
    handler = view._route.handler
    assert handler.__func__ is IndexView.get, (
        "the cached ResourceRoute still holds the pre-patch handler — the "
        "class patch never reached the live route, so `/` serves Home "
        "Assistant and the label QR code misses the wizard"
    )
    response = await handler(SimpleNamespace())
    assert response.status == 302
    assert response.headers["location"] == SETUP_PATH


async def test_redirect_fires_when_nothing_was_cached_yet(unpatched_index_view):
    """The easy ordering must keep working — a fix that only handles the
    late case would break the case that already worked."""
    view = IndexView(None, None)  # type: ignore[arg-type]
    hass = _fake_hass(view, completed=False)
    view.hass = hass  # type: ignore[assignment]

    _patch_index_view_for_wizard_redirect(hass)

    _handler, response = await _handler_response(view)
    assert response.status == 302
    assert response.headers["location"] == SETUP_PATH


async def test_completed_wizard_falls_through_to_home_assistant(
    unpatched_index_view,
):
    """Proof the guard can also go GREEN: a finished wizard must NOT redirect.

    Without this, a patch that always redirects would pass the two tests above
    and lock every provisioned device out of its own dashboard.
    """
    view = IndexView(None, None)  # type: ignore[arg-type]
    hass = _fake_hass(view, completed=True)
    view.hass = hass  # type: ignore[assignment]
    view._route  # noqa: B018  - same precondition as the regression case

    _patch_index_view_for_wizard_redirect(hass)

    handler = view._route.handler
    called: list[bool] = []

    async def _original(self, request):  # pragma: no cover - replaced below
        called.append(True)

    # We do not want to render Core's real template here; assert instead that
    # the patched handler does NOT short-circuit with our redirect.
    response = None
    try:
        response = await handler(SimpleNamespace())
    except Exception:  # noqa: BLE001 - Core's get needs a real hass; fine
        pass
    if response is not None:
        assert response.headers.get("location") != SETUP_PATH, (
            "a completed wizard must fall through to Home Assistant"
        )


# --- The redirect must carry the label's QR parameters ------------------------
#
# Second defect, found 2026-09-10 while reviewing the fix above. The device
# label's QR code encodes
#     http://<prefix>.ki-butler.greenautarky.com/?pin=<pin>&device=<id>
# and the setup panel reads both back out of `window.location`
# (ha-panel-greenautarky-setup.ts: `URLSearchParams` -> `_autoPin` ->
# ga-setup-pin.ts auto-fills the six digits). The redirect answered with a bare
# `/greenautarky-setup.html`, so it threw the PIN away and the customer had to
# read it off the label and type it in — the one thing the QR code exists to
# avoid. Invisible because every check only ever asked for the STATUS and the
# PATH, never whether the parameters survived.
#
# Not a regression of the cached-route fix: it was true on every device where
# the redirect fired at all, including the one that looked healthy.


def _pending_view():
    view = IndexView(None, None)  # type: ignore[arg-type]
    hass = _fake_hass(view, completed=False)
    view.hass = hass  # type: ignore[assignment]
    _patch_index_view_for_wizard_redirect(hass)
    return view


async def _location(view: IndexView, query: dict) -> str:
    response = await view._route.handler(SimpleNamespace(query=query))
    return response.headers["location"]


async def test_label_qr_pin_and_device_survive_the_redirect(unpatched_index_view):
    """The customer scanned the QR code; the PIN must reach the panel."""
    location = await _location(
        _pending_view(), {"pin": "123456", "device": "KIB-SON-00000000"}
    )
    assert location == f"{SETUP_PATH}?pin=123456&device=KIB-SON-00000000", (
        "the label QR code carries ?pin=&device= and the setup panel auto-fills "
        "the six digits from it — a redirect to a bare path makes the customer "
        "type a PIN they already scanned"
    )


async def test_no_query_still_redirects_to_the_bare_path(unpatched_index_view):
    """Someone typing the bare host must not get a stray `?`."""
    assert await _location(_pending_view(), {}) == SETUP_PATH


async def test_only_pin_and_device_are_forwarded(unpatched_index_view):
    """Allowlist, not pass-through.

    The query string is customer-controlled and lands in a `Location` header.
    Forwarding it wholesale would reflect anything an attacker can put in a
    link — so this pins the two parameters the consumer actually reads and
    proves the rest is dropped, header-injection and open-redirect shapes
    included.
    """
    location = await _location(
        _pending_view(),
        {"pin": "123456", "evil": "x\r\nSet-Cookie: a=b", "next": "//attacker.example"},
    )
    assert location == f"{SETUP_PATH}?pin=123456"


async def test_forwarded_values_are_encoded_not_pasted(unpatched_index_view):
    """Whatever arrived is re-encoded, never concatenated raw."""
    location = await _location(_pending_view(), {"device": "KIB SON/39"})
    assert location == f"{SETUP_PATH}?device=KIB+SON%2F39"
