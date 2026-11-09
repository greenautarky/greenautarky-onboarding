"""NEW RESIDENT = NEW CONSENT: a reset also clears the telemetry preferences and
the PIN proof (Odoo #1695, Thomas's decision of 2026-11-09).

``test_reset_clears_consents.py`` covers ``consents``. A reset that cleared only
that left the previous resident's consent-based processing running: their Tier 2
opt-in stayed in ``greenautarky_telemetry``, and their ``pin_verified`` stayed
true so the next person never proved physical access.

The telemetry component is a stand-in whose ``async_set_preferences`` writes the
v2 tiers the way the real one does; the assertions read the TIERS (what the OS
gate reads), not the call log.
"""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest

from greenautarky_site import PANEL_URL_PATH
from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import (
    GAOnboardingGDPRView,
    GAOnboardingResetView,
)

TELEMETRY = "greenautarky_telemetry"


class _FakeStore:
    async def async_save(self, data):
        self.saved = data


class _User:
    def __init__(self, is_admin: bool) -> None:
        self.is_admin = is_admin
        self.name = "qa-admin"


class _FakeRequest(dict):
    def __init__(self, hass, body=None, user=None):
        super().__init__()
        self.app = {"hass": hass}
        self._body = body or {}
        if user is not None:
            self["hass_user"] = user

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _panel_registered(hass):
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})[PANEL_URL_PATH] = object()


def _previous_resident(hass) -> dict:
    """Finished run: PIN proven, Tier 1 opted OUT, Tier 2 opted IN."""
    st = {
        "completed": True,
        "gdpr_accepted": True,
        "steps_done": ["pin", "gdpr", "account", "telemetry", "complete"],
        "consents": {"gdpr": {"version": 1, "accepted_at": "2026-11-09T00:00:00+00:00"}},
        "pin_verified": True,
        "pin_attempts": 2,
    }
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    hass.data[TELEMETRY] = {
        "store": _FakeStore(),
        "preferences": {
            "policy_version_accepted": 1,
            "tiers": {
                "tier1": {"value": False, "accepted_at": "x", "policy_version": 1},
                "tier2": {"value": True, "accepted_at": "x", "policy_version": 1},
            },
            "legacy": {"error_logs": False, "metrics": True},
        },
    }
    return st


def _install_telemetry(monkeypatch):
    async def async_set_preferences(hass, **flags):
        prefs = hass.data[TELEMETRY]["preferences"]
        if "error_logs" in flags:
            prefs["tiers"]["tier1"]["value"] = bool(flags["error_logs"])
        if "metrics" in flags:
            prefs["tiers"]["tier2"]["value"] = bool(flags["metrics"])
        await hass.data[TELEMETRY]["store"].async_save(prefs)
        return prefs

    mod = types.ModuleType(f"custom_components.{TELEMETRY}")
    mod.async_set_preferences = async_set_preferences
    pkg = sys.modules.get("custom_components") or types.ModuleType("custom_components")
    monkeypatch.setitem(sys.modules, "custom_components", pkg)
    monkeypatch.setitem(sys.modules, f"custom_components.{TELEMETRY}", mod)


async def test_reset_returns_the_telemetry_tiers_to_the_wizard_defaults(hass, monkeypatch):
    _install_telemetry(monkeypatch)
    _previous_resident(hass)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))

    assert resp.status == 200
    tiers = hass.data[TELEMETRY]["preferences"]["tiers"]
    assert tiers["tier2"]["value"] is False, "the previous resident's Tier 2 opt-in survived the reset"
    assert tiers["tier1"]["value"] is True, "Tier 1 must be back at its default (on)"
    assert hass.data[TELEMETRY]["store"].saved is hass.data[TELEMETRY]["preferences"]
    assert json.loads(resp.body)["telemetry_reset"] == "reset"


async def test_reset_clears_pin_verified_and_a_gated_step_refuses_again(hass, monkeypatch):
    _install_telemetry(monkeypatch)
    st = _previous_resident(hass)
    # A device with a provisioned PIN: the REAL gate runs, not a stub of it.
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: True)

    await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))

    assert st["pin_verified"] is False
    resp = await GAOnboardingGDPRView().post(_FakeRequest(hass, {"accepted": True}))
    assert resp.status == 403, "a gated step answered without a fresh PIN proof after the reset"
    # The brute-force counters are not a resident's to wipe.
    assert st["pin_attempts"] == 2


async def test_a_non_admin_reset_leaves_telemetry_and_pin_alone(hass, monkeypatch):
    _install_telemetry(monkeypatch)
    st = _previous_resident(hass)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(False)))

    assert resp.status == 403
    assert st["pin_verified"] is True
    assert hass.data[TELEMETRY]["preferences"]["tiers"]["tier2"]["value"] is True


async def test_reset_on_a_device_without_telemetry_still_works(hass, monkeypatch):
    """Must-pass: no telemetry component -> nothing to reset, reset succeeds."""
    st = _previous_resident(hass)
    hass.data.pop(TELEMETRY)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))

    assert resp.status == 200
    assert json.loads(resp.body)["telemetry_reset"] == "absent"
    assert st["completed"] is False and st["pin_verified"] is False


async def test_an_old_telemetry_component_is_reset_loudly(hass, monkeypatch, caplog):
    monkeypatch.delitem(sys.modules, f"custom_components.{TELEMETRY}", raising=False)
    monkeypatch.delitem(sys.modules, TELEMETRY, raising=False)
    _previous_resident(hass)

    with caplog.at_level(logging.WARNING):
        resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))

    assert resp.status == 200
    assert json.loads(resp.body)["telemetry_reset"] == "legacy"
    assert any("older than 0.2.4" in r.message for r in caplog.records)


async def test_a_telemetry_failure_fails_the_reset_and_changes_nothing(hass, monkeypatch):
    async def boom(hass, **flags):
        raise RuntimeError("storage unavailable")

    mod = types.ModuleType(f"custom_components.{TELEMETRY}")
    mod.async_set_preferences = boom
    pkg = sys.modules.get("custom_components") or types.ModuleType("custom_components")
    monkeypatch.setitem(sys.modules, "custom_components", pkg)
    monkeypatch.setitem(sys.modules, f"custom_components.{TELEMETRY}", mod)
    st = _previous_resident(hass)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))

    assert resp.status == 500
    assert st["completed"] is True and st["pin_verified"] is True and st["consents"]
