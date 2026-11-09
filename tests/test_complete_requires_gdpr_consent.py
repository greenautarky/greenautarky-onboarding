"""`complete` must not close the wizard on a device with no GDPR consent recorded.

Odoo #1004, F-A: measured live on a bench device (2026-09-14), `POST /complete`
answered 200 and persisted ``completed: true`` while ``consents`` was empty and
``/consent/status`` listed ``gdpr`` as outdated. F-B (same ticket): the wizard's
own GDPR step only set the legacy ``gdpr_accepted`` boolean, so the versioned
``consents.gdpr`` that the gate reads was never written by the wizard itself.

The subject is the versioned record (``consents.gdpr``), not the legacy boolean
and not the ``steps_done`` entry: a step marker records that a page was visited.
"""

import pytest

from greenautarky_site.const import CONSENT_TYPES, DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import (
    GAOnboardingCompleteView,
    GAOnboardingGDPRView,
)

pytestmark = pytest.mark.usefixtures("pin_not_under_test")


class _FakeStore:
    def __init__(self) -> None:
        self.saved = None

    async def async_save(self, data):
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body):
        self.app = {"hass": hass}
        self._body = body

    async def json(self):
        return self._body


def _seed(hass, *, steps_done, consents) -> dict:
    st = {
        "completed": False,
        "gdpr_accepted": False,
        "steps_done": list(steps_done),
        "consents": consents,
        "pin_verified": True,
    }
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


@pytest.fixture(autouse=True)
def _pin_is_satisfied(monkeypatch):
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)


@pytest.fixture
def panels(hass):
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})


def _gdpr(version=None):
    v = CONSENT_TYPES["gdpr"] if version is None else version
    return {"version": v, "accepted_at": "2026-11-09T00:00:00+00:00"}


@pytest.mark.parametrize(
    "consents, why",
    [
        ({}, "no consent recorded at all"),
        ({"ethernet": {"version": 1}}, "another consent, but not GDPR"),
        ({"gdpr": _gdpr(0)}, "a GDPR record older than the required version"),
        ({"gdpr": {}}, "an empty GDPR record"),
    ],
)
async def test_complete_refuses_without_gdpr_consent(hass, panels, consents, why):
    # Every other step is done: the ONLY missing thing is the consent.
    st = _seed(hass, steps_done=["gdpr", "account"], consents=consents)
    st["gdpr_accepted"] = True  # the legacy flag must not satisfy the gate

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 409, f"{why}: complete answered {resp.status}"
    assert b"gdpr" in resp.body.lower(), f"{why}: the refusal does not name the consent"
    assert st["completed"] is False, f"{why}: the one-way door closed anyway"
    assert "complete" not in st["steps_done"]
    assert hass.data[DOMAIN]["store"].saved is None, f"{why}: state written behind a refusal"


async def test_complete_works_with_the_gdpr_consent_recorded(hass, panels):
    """The gate must be able to open."""
    st = _seed(hass, steps_done=["gdpr", "account"], consents={"gdpr": _gdpr()})

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 200
    assert st["completed"] is True
    assert "complete" in st["steps_done"]


async def test_ethernet_is_a_choice_not_a_precondition(hass, panels):
    """Ethernet records on OR off; skipping it must not strand a device."""
    st = _seed(hass, steps_done=["gdpr", "account"], consents={"gdpr": _gdpr()})
    assert "ethernet" not in st["consents"]

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 200


async def test_the_wizard_gdpr_step_writes_the_versioned_consent(hass, panels):
    """F-B: the real wizard path (gdpr step, then complete) must reach 200.
    Before this change the step set only `gdpr_accepted`."""
    st = _seed(hass, steps_done=["account"], consents={})

    resp = await GAOnboardingGDPRView().post(_FakeRequest(hass, {"accepted": True}))
    assert resp.status == 200
    assert st["gdpr_accepted"] is True
    assert st["consents"]["gdpr"]["version"] == CONSENT_TYPES["gdpr"]

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))
    assert resp.status == 200
    assert st["completed"] is True


@pytest.mark.parametrize("body", [{"accepted": False}, {}])
async def test_declining_gdpr_records_nothing_and_does_not_unlock_complete(
    hass, panels, body
):
    st = _seed(hass, steps_done=["account"], consents={})

    resp = await GAOnboardingGDPRView().post(_FakeRequest(hass, body))
    assert resp.status == 400
    assert "gdpr" not in st["consents"]
    assert "gdpr" not in st["steps_done"]
    assert st["gdpr_accepted"] is False

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))
    assert resp.status == 409
    assert st["completed"] is False
