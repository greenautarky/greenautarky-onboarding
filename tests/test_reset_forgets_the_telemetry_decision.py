"""A reset forgets the previous resident's telemetry decision; it does not record one.

The telemetry component's ``async_set_preferences`` records a DECISION: it
stamps ``accepted_at`` and the policy version and saves. A reset that used it
to put the tiers back to their starting values left a record of a choice the
next resident never made, and the OS gate acts on records. With no record the
gate sends neither tier, as on a freshly flashed device.

The stand-in keeps a STORED record the way the real component does (a save
writes it, a removal deletes it); the assertions read that record.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from greenautarky_site import PANEL_URL_PATH
from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding.wizard import GAOnboardingResetView

TELEMETRY = "greenautarky_telemetry"


class _Store:
    def __init__(self, stored):
        self.stored = stored

    async def async_save(self, data):
        self.stored = json.loads(json.dumps(data))


class _TelemetryStore(_Store):
    async def async_remove(self):
        self.stored = None


class _User:
    is_admin = True
    name = "qa-admin"


class _Request(dict):
    def __init__(self, hass):
        super().__init__(hass_user=_User())
        self.app = {"hass": hass}


@pytest.fixture(autouse=True)
def _panel_registered(hass):
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})[PANEL_URL_PATH] = object()


def _record(tier1, tier2):
    tier = lambda v: {"value": v, "accepted_at": "2026-11-09T00:00:00+00:00", "policy_version": 1}  # noqa: E731
    return {"policy_version_accepted": 1, "tiers": {"tier1": tier(tier1), "tier2": tier(tier2)},
            "legacy": {"error_logs": tier1, "metrics": tier2}}


def _device(hass, *, tier1=False, tier2=True):
    """The previous resident finished the wizard and decided."""
    hass.data[DOMAIN] = {"store": _Store(None), "state": {
        "completed": True, "gdpr_accepted": True, "steps_done": ["pin", "gdpr", "account", "complete"],
        "consents": {"gdpr": {"version": 1}}}}
    hass.data[TELEMETRY] = {"store": _TelemetryStore(_record(tier1, tier2)), "preferences": _record(tier1, tier2)}
    return hass.data[TELEMETRY]["store"]


def _component(monkeypatch, *, clear=True):
    """The telemetry component as it behaves: set records a decision, clear removes it."""
    async def async_set_preferences(hass, **flags):
        rec = _record(bool(flags.get("error_logs", True)), bool(flags.get("metrics", False)))
        hass.data[TELEMETRY]["preferences"] = rec
        await hass.data[TELEMETRY]["store"].async_save(rec)
        return rec

    async def async_clear_preferences(hass):
        rec = _record(True, False)
        rec["policy_version_accepted"] = None
        for t in rec["tiers"].values():
            t["accepted_at"] = t["policy_version"] = None
        hass.data[TELEMETRY]["preferences"] = rec
        await hass.data[TELEMETRY]["store"].async_remove()
        return rec

    mod = types.ModuleType(f"custom_components.{TELEMETRY}")
    mod.async_set_preferences = async_set_preferences
    if clear:
        mod.async_clear_preferences = async_clear_preferences
    monkeypatch.setitem(sys.modules, "custom_components", sys.modules.get("custom_components") or types.ModuleType("custom_components"))
    monkeypatch.setitem(sys.modules, f"custom_components.{TELEMETRY}", mod)


@pytest.mark.parametrize("tier1, tier2", [(False, True), (True, False), (True, True), (False, False)])
async def test_after_a_reset_no_telemetry_decision_is_stored(hass, monkeypatch, tier1, tier2):
    _component(monkeypatch)
    store = _device(hass, tier1=tier1, tier2=tier2)

    resp = await GAOnboardingResetView().post(_Request(hass))

    assert resp.status == 200
    assert store.stored is None, f"a decision is stored after the reset: {store.stored}"


async def test_after_a_reset_nothing_in_memory_reads_as_accepted(hass, monkeypatch):
    _component(monkeypatch)
    _device(hass)

    await GAOnboardingResetView().post(_Request(hass))

    prefs = hass.data[TELEMETRY]["preferences"]
    assert prefs["policy_version_accepted"] is None
    assert all(t["accepted_at"] is None for t in prefs["tiers"].values())


async def test_a_component_that_cannot_forget_refuses_the_reset_and_changes_nothing(hass, monkeypatch):
    """Recording the defaults is not an option, so the reset refuses with the code
    callers already handle, and leaves the wizard and the record as they were."""
    _component(monkeypatch, clear=False)
    store = _device(hass)
    before = json.dumps(store.stored, sort_keys=True), json.dumps(hass.data[DOMAIN]["state"], sort_keys=True)

    resp = await GAOnboardingResetView().post(_Request(hass))

    assert resp.status == 500
    assert json.loads(resp.body)["code"] == "telemetry_component_outdated"
    assert (json.dumps(store.stored, sort_keys=True), json.dumps(hass.data[DOMAIN]["state"], sort_keys=True)) == before


async def test_the_reset_reports_what_it_did(hass, monkeypatch):
    _component(monkeypatch)
    _device(hass)
    resp = await GAOnboardingResetView().post(_Request(hass))
    assert json.loads(resp.body)["telemetry_reset"] == "cleared"
