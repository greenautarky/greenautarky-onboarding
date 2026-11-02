"""A device without a PIN file refuses the wizard instead of opening every step.

THE DEFECT THIS FILE EXISTS FOR (found in the security-concept review, 2026-11-02).

``_check_pin_verified`` treated a missing PIN file as "no verification needed":
on a device where the file was absent, every gated wizard step — account, GDPR,
telemetry, ethernet, complete — ran without any proof of physical access. A
missing file is not a reason to trust the caller; it is a device that has not
been provisioned yet.

Every provisioned device carries the PIN file (written at provisioning) and a
tenant wipe keeps it, so the only devices without it are not provisioned yet.

Same pattern as ``test_wizard_steps_are_gated_by_the_pin.py``: the views are
called directly; the file is simulated by patching ``_pin_required`` — the exact
predicate the gate reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import (
    GAOnboardingCompleteView,
    GAOnboardingEthernetView,
    GAOnboardingStatusView,
    GAOnboardingTelemetryView,
)


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        pass


class _FakeRequest:
    def __init__(self, hass, body=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self.cookies: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _seed(hass, *, pin_verified: bool = False) -> dict:
    st = {"completed": False, "steps_done": ["account"], "consents": {}, "pin_verified": pin_verified}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


VIEWS = [
    pytest.param(GAOnboardingCompleteView, {}, id="complete"),
    pytest.param(GAOnboardingTelemetryView, {"error_logs": True}, id="telemetry"),
    pytest.param(GAOnboardingEthernetView, {"enable_ethernet": False}, id="ethernet"),
]


@pytest.mark.parametrize("view, body", VIEWS)
async def test_a_device_without_a_pin_file_refuses_every_gated_step(hass, monkeypatch, view, body):
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)
    st = _seed(hass)
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in st.items()}

    resp = await view().post(_FakeRequest(hass, body))

    assert resp.status == 403, f"{view.__name__} answered {resp.status} on a device without a PIN file"
    assert st["completed"] is False
    assert st["steps_done"] == before["steps_done"]


async def test_the_refusal_says_the_device_is_not_provisioned(hass, monkeypatch):
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)
    _seed(hass)
    resp = await GAOnboardingTelemetryView().post(_FakeRequest(hass, {"error_logs": True}))
    assert resp.status == 403
    assert b"pin_not_provisioned" in resp.body


async def test_a_missing_pin_file_does_not_hide_the_pin_step(hass, monkeypatch):
    """The wizard decides from `pin_required` whether to show the PIN page. If it
    said False for a missing file, the wizard would skip the PIN page and then hit
    the refusal at the next step with nothing to enter."""
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)
    _seed(hass)
    resp = await GAOnboardingStatusView().get(_FakeRequest(hass))
    import json
    data = json.loads(resp.body)
    assert data["pin_required"] is True
    assert data["pin_provisioned"] is False
