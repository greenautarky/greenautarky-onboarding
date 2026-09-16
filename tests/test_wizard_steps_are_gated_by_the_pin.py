"""Every state-changing wizard step stands behind the PIN, not just the two that did.

THE DEFECT THIS FILE EXISTS FOR, measured on a bench device on 2026-09-16.

The wizard's PIN proves physical access: the code is printed on the device, and
the account step and the GDPR step refuse to run until it has been entered.
Three other steps changed state without asking — ``telemetry``, ``ethernet`` and
``complete``. ``complete`` is the expensive one: an unauthenticated
``POST /api/greenautarky_site/complete`` with an empty body answered 200, set
``completed: true`` and removed the wizard panel — on a device with NO resident
account yet. The device was then set up, from the wizard's point of view, and
had nobody who could log in. Getting it back required an admin token, which the
resident does not have.

Found because a scripted onboarding run sent empty payloads by mistake: every
gated step answered 403 as it should, and ``complete`` answered 200.

Written to the pattern the rest of this suite uses: the view is called directly
with a fake request. The PIN file is simulated by patching ``_pin_required``,
which is the exact predicate the gate reads — a test that seeded a file would
be testing the filesystem, not the gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import (
    GAOnboardingCompleteView,
    GAOnboardingEthernetView,
    GAOnboardingTelemetryView,
)


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _seed(hass, *, pin_verified: bool) -> dict:
    st = {"completed": False, "steps_done": [], "consents": {}, "pin_verified": pin_verified}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


VIEWS = [
    pytest.param(GAOnboardingCompleteView, {}, id="complete"),
    pytest.param(GAOnboardingTelemetryView, {"error_logs": True}, id="telemetry"),
    pytest.param(GAOnboardingEthernetView, {"enable_ethernet": False}, id="ethernet"),
]


@pytest.mark.parametrize("view, body", VIEWS)
async def test_a_step_refuses_until_the_pin_is_verified(hass, monkeypatch, view, body):
    """A PIN is required and has not been entered: the step must answer 403 and
    leave the state exactly as it was. `complete` in particular must not flip
    `completed` — that is the one-way door measured on the bench."""
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: True)
    st = _seed(hass, pin_verified=False)
    before = dict(st)

    resp = await view().post(_FakeRequest(hass, body))

    assert resp.status == 403, f"{view.__name__} answered {resp.status} without a PIN"
    assert st == before, f"{view.__name__} changed state behind a refused request"
    assert hass.data[DOMAIN]["store"].saved is None


async def test_complete_still_works_once_the_pin_is_verified(hass, monkeypatch):
    """The gate must be able to open, too — a step that refuses everyone is not a
    gate. With the PIN verified, `complete` does what it always did."""
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: True)
    # `complete` removes the wizard panel; that needs the frontend's panel table.
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})
    st = _seed(hass, pin_verified=True)

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 200
    assert st["completed"] is True
    assert "complete" in st["steps_done"]


async def test_no_pin_file_means_no_gate(hass, monkeypatch):
    """A device without a PIN file (pre-1.0 images, some benches) keeps working:
    the gate is the PIN's, not an extra login."""
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})
    st = _seed(hass, pin_verified=False)

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 200
    assert st["completed"] is True
