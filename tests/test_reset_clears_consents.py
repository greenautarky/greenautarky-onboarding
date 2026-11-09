"""An onboarding reset starts a new resident's run, so it clears the consents.

Odoo #1695 (decision 2026-11-09): ``GAOnboardingResetView`` used to preserve
``consents``. After a reset the previous resident's ``consents.gdpr`` still
satisfied the gate on ``POST /complete`` (Odoo #1004), so a new run could finish
without the new resident ever accepting. A consent is personal: new resident,
new consent.

The subject is the REAL reset view and the REAL complete view over one shared
state, not a re-declared copy of either.
"""

import pytest

from greenautarky_site import PANEL_URL_PATH
from greenautarky_site.const import CONSENT_TYPES, DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import (
    GAOnboardingCompleteView,
    GAOnboardingGDPRView,
    GAOnboardingResetView,
)

pytestmark = pytest.mark.usefixtures("pin_not_under_test")


class _FakeStore:
    async def async_save(self, data):
        self.saved = data


class _User:
    def __init__(self, is_admin: bool) -> None:
        self.is_admin = is_admin
        self.name = "qa-admin"


class _FakeRequest(dict):
    """aiohttp request stand-in: ``request.app``, ``request[...]``, ``json()``."""

    def __init__(self, hass, body=None, user=None):
        super().__init__()
        self.app = {"hass": hass}
        self._body = body or {}
        if user is not None:
            self["hass_user"] = user

    async def json(self):
        return self._body


def _finished_run(hass) -> dict:
    """A device whose previous resident finished onboarding and consented."""
    st = {
        "completed": True,
        "gdpr_accepted": True,
        "steps_done": ["gdpr", "account", "complete"],
        "consents": {
            "gdpr": {
                "version": CONSENT_TYPES["gdpr"],
                "accepted_at": "2026-11-09T00:00:00+00:00",
            }
        },
        "pin_verified": True,
    }
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


@pytest.fixture(autouse=True)
def _pin_is_satisfied(monkeypatch):
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)


@pytest.fixture(autouse=True)
def _panel_registered(hass):
    from homeassistant.components.frontend import DATA_PANELS

    # The reset view re-registers the sidebar panel unless it is present.
    hass.data.setdefault(DATA_PANELS, {})[PANEL_URL_PATH] = object()


async def test_reset_clears_the_consents_and_complete_then_refuses(hass):
    st = _finished_run(hass)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))
    assert resp.status == 200
    assert st["completed"] is False
    assert st["steps_done"] == []
    assert st["consents"] == {}, "the previous resident's consent survived the reset"

    # The new run reaches /complete with an account but without consenting.
    st["steps_done"].append("account")
    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass))
    assert resp.status == 409
    assert b"consent_missing" in resp.body
    assert st["completed"] is False


async def test_a_new_resident_who_consents_can_complete_after_a_reset(hass):
    """Must-pass: the gate still opens for the new run."""
    st = _finished_run(hass)
    await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(True)))
    st["steps_done"].append("account")

    resp = await GAOnboardingGDPRView().post(_FakeRequest(hass, {"accepted": True}))
    assert resp.status == 200
    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass))
    assert resp.status == 200
    assert st["completed"] is True


async def test_a_non_admin_reset_leaves_the_consents_alone(hass):
    st = _finished_run(hass)

    resp = await GAOnboardingResetView().post(_FakeRequest(hass, user=_User(False)))

    assert resp.status == 403
    assert st["consents"]["gdpr"]["version"] == CONSENT_TYPES["gdpr"]
    assert st["completed"] is True
