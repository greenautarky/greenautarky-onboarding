"""The Ethernet step must record WHAT the resident chose, not only that a step ran.

Until 2.12.3 ``consents.ethernet`` was ``{version, accepted_at}`` whether the
switch was on or off: a consent record with no value. Found on the
BOSv1.5.1-rc4 bench run (2026-10-13). The stored record now carries
``value: true|false``. Readers that only know ``version``/``accepted_at`` keep
working (additive key); records written before 2.12.3 have no ``value``.
"""

from __future__ import annotations

from typing import Any

import pytest

from greenautarky_site.consent import get_outdated_consents
from greenautarky_site.const import CONSENT_TYPES, DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding import wizard as wizard_module
from greenautarky_site.onboarding.wizard import GAOnboardingEthernetView


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        pass


class _FakeRequest:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def seeded(hass, monkeypatch):
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)
    st: dict[str, Any] = {"completed": False, "steps_done": [], "consents": {}}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    # The enable branch shells out to ga-manage-ethernet; not the subject here.
    monkeypatch.setattr(wizard_module.subprocess, "run", lambda *a, **k: None)
    return st


@pytest.mark.parametrize("chosen", [True, False])
async def test_the_chosen_value_is_stored(hass, seeded, chosen):
    resp = await GAOnboardingEthernetView().post(
        _FakeRequest(hass, {"enable_ethernet": chosen})
    )
    assert resp.status == 200
    rec = seeded["consents"]["ethernet"]
    assert rec["value"] is chosen, f"switch={chosen} but the record says {rec!r}"


async def test_an_absent_field_is_stored_as_off(hass, seeded):
    await GAOnboardingEthernetView().post(_FakeRequest(hass, {}))
    assert seeded["consents"]["ethernet"]["value"] is False


async def test_off_and_on_records_differ(hass, seeded):
    """Guards the original defect directly: the two choices were identical."""
    await GAOnboardingEthernetView().post(
        _FakeRequest(hass, {"enable_ethernet": False})
    )
    off = dict(seeded["consents"]["ethernet"])
    seeded["steps_done"].clear()
    await GAOnboardingEthernetView().post(_FakeRequest(hass, {"enable_ethernet": True}))
    on = seeded["consents"]["ethernet"]
    assert off["value"] != on["value"]


async def test_legacy_and_new_records_are_both_current(hass, seeded):
    """Backwards compatibility: a pre-2.12.3 record (no ``value``) and a new one
    are both current for the version-tracking reader; neither raises a repair."""
    v = CONSENT_TYPES["ethernet"]
    legacy = {"ethernet": {"version": v, "accepted_at": "2026-10-01T00:00:00+00:00"}}
    assert "ethernet" not in get_outdated_consents({"consents": legacy})
    await GAOnboardingEthernetView().post(
        _FakeRequest(hass, {"enable_ethernet": False})
    )
    assert "ethernet" not in get_outdated_consents(seeded)
    assert {"version", "accepted_at"} <= set(seeded["consents"]["ethernet"])
