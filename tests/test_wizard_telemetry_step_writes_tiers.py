"""The telemetry step records a CONSENT, not two flat booleans.

THE DEFECT THIS FILE EXISTS FOR, measured on a bench device on 2026-09-16.

The wizard's telemetry step wrote ``error_logs`` / ``metrics`` as flat keys
straight into ``greenautarky_telemetry``'s preferences dict and saved it. That
component's v2 record keeps the truth in ``tiers.tier1.value`` /
``tiers.tier2.value`` — which the flat write never touched — and the OS gate
(``ga-telemetry-gate``) reads the tiers. The resident answered yes to Tier 2;
the store said ``tier2: false`` next to ``metrics: true``. Tier 1 looked right
only because its default is ``True``.

Since greenautarky_telemetry 0.2.4 there is one entry point for a decision,
``async_set_preferences``. The step calls it. On a device that still ships an
older telemetry component the step falls back to the flat write and SAYS SO at
WARNING — a silent fallback would be this defect again, with a different name.

The telemetry component is simulated as a module in ``sys.modules`` with a
setter that behaves like the real one for the part under test (it writes the
tiers). A fake that only records the call would prove the call, not the
outcome; the outcome — tiers written — is what the gate reads.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding.wizard import GAOnboardingTelemetryView

TELEMETRY = "greenautarky_telemetry"


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


def _seed(hass) -> dict:
    """Wizard in progress (PIN done) and a v2 telemetry record at its defaults —
    exactly what a fresh device holds when the resident reaches this step."""
    hass.data[DOMAIN] = {
        "store": _FakeStore(),
        "state": {"completed": False, "steps_done": ["pin"], "consents": {}, "pin_verified": True},
    }
    prefs = {
        "policy_version_accepted": None,
        "tiers": {
            "tier1": {"value": True, "accepted_at": None, "policy_version": None},
            "tier2": {"value": False, "accepted_at": None, "policy_version": None},
        },
        "legacy": {"error_logs": True, "metrics": False},
    }
    hass.data[TELEMETRY] = {"store": _FakeStore(), "preferences": prefs}
    return prefs


def _install_fake_telemetry(monkeypatch) -> list[dict[str, Any]]:
    """A stand-in for greenautarky_telemetry >= 0.2.4: `async_set_preferences`
    writes the tiers the way the real one does, and records what it was asked."""
    calls: list[dict[str, Any]] = []

    async def async_set_preferences(hass, **flags):
        calls.append(flags)
        prefs = hass.data[TELEMETRY]["preferences"]
        if "error_logs" in flags:
            prefs["tiers"]["tier1"]["value"] = bool(flags["error_logs"])
        if "metrics" in flags:
            prefs["tiers"]["tier2"]["value"] = bool(flags["metrics"])
        prefs["policy_version_accepted"] = 1
        await hass.data[TELEMETRY]["store"].async_save(prefs)
        return prefs

    mod = types.ModuleType(f"custom_components.{TELEMETRY}")
    mod.async_set_preferences = async_set_preferences
    pkg = sys.modules.get("custom_components") or types.ModuleType("custom_components")
    monkeypatch.setitem(sys.modules, "custom_components", pkg)
    monkeypatch.setitem(sys.modules, f"custom_components.{TELEMETRY}", mod)
    return calls


async def test_yes_to_tier2_lands_in_the_tiers(hass, monkeypatch):
    calls = _install_fake_telemetry(monkeypatch)
    prefs = _seed(hass)

    resp = await GAOnboardingTelemetryView().post(
        _FakeRequest(hass, {"error_logs": True, "metrics": True})
    )

    assert resp.status == 200
    assert prefs["tiers"]["tier2"]["value"] is True, (
        "the resident said yes to Tier 2 and the record the gate reads still says no"
    )
    assert prefs["tiers"]["tier1"]["value"] is True
    assert calls == [{"error_logs": True, "metrics": True}]
    assert hass.data[TELEMETRY]["store"].saved is prefs


async def test_no_to_tier1_is_recorded_too(hass, monkeypatch):
    """Opt-OUT of Tier 1 is the other direction of the same write."""
    _install_fake_telemetry(monkeypatch)
    prefs = _seed(hass)

    await GAOnboardingTelemetryView().post(
        _FakeRequest(hass, {"error_logs": False, "metrics": False})
    )

    assert prefs["tiers"]["tier1"]["value"] is False
    assert prefs["tiers"]["tier2"]["value"] is False


async def test_an_old_telemetry_component_gets_the_flat_write_and_a_loud_warning(
    hass, monkeypatch, caplog
):
    """No `async_set_preferences` on the device → the step must not crash the
    wizard, must still record what it can, and must SAY the record is partial."""
    monkeypatch.delitem(sys.modules, f"custom_components.{TELEMETRY}", raising=False)
    prefs = _seed(hass)

    with caplog.at_level(logging.WARNING):
        resp = await GAOnboardingTelemetryView().post(
            _FakeRequest(hass, {"error_logs": True, "metrics": True})
        )

    assert resp.status == 200
    assert prefs["metrics"] is True  # the legacy flat write, as before
    assert any("async_set_preferences" in r.message for r in caplog.records), (
        "the fallback happened silently — that is the defect with another name"
    )
