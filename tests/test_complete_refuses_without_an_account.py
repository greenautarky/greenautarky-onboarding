"""`complete` is a one-way door, so it must not close on an empty room.

Measured 2026-09-22 on a bench device: `create_user` was rejected for a missing
`client_id`, the flow carried on, and `complete` accepted the PIN and set
`completed: true`. The device then had the converge-created admin and NO
resident, and `create_user` answered "Onboarding already completed" for ever —
the only ways back were an admin token or a reflash.

The 2.9.1 PIN gate cannot catch this: the caller had the PIN. What was missing
is order.
"""

from unittest.mock import MagicMock

import pytest

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding.wizard import GAOnboardingCompleteView


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


def _seed(hass, *, steps_done) -> dict:
    st = {
        "completed": False,
        "steps_done": list(steps_done),
        "consents": {},
        "pin_verified": True,
    }
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


@pytest.fixture(autouse=True)
def _pin_is_satisfied(monkeypatch):
    # The PIN is not the subject here: every case below has it verified.
    monkeypatch.setattr(pin_module, "_pin_required", lambda _hass: False)


@pytest.mark.parametrize(
    "steps_done, name",
    [
        ([], "nothing done at all"),
        (["ethernet"], "the shape the bench device was in"),
        (["gdpr", "ethernet", "telemetry"], "every step except the account"),
    ],
)
async def test_complete_refuses_while_no_account_step_is_recorded(
    hass, steps_done, name
):
    st = _seed(hass, steps_done=steps_done)

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 409, f"{name}: complete answered {resp.status}"
    assert st["completed"] is False, f"{name}: the one-way door closed anyway"
    assert "complete" not in st["steps_done"]
    assert hass.data[DOMAIN]["store"].saved is None, (
        f"{name}: state was written behind a refused request"
    )


async def test_complete_works_once_the_account_step_is_recorded(hass):
    """The guard must be able to open — a check that refuses everyone is not a
    gate, it is an outage."""
    from homeassistant.components.frontend import DATA_PANELS

    hass.data.setdefault(DATA_PANELS, {})
    st = _seed(hass, steps_done=["gdpr", "account"])

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 200
    assert st["completed"] is True
    assert "complete" in st["steps_done"]


async def test_the_guard_reads_the_recorded_step_not_the_user_count(hass):
    """A device always carries the converge-created admin, so counting users
    would pass on exactly the device this guard exists for. The subject is what
    the account step RECORDED."""
    hass.auth = MagicMock()
    hass.auth.async_get_users = MagicMock(
        return_value=[MagicMock(is_admin=True, system_generated=False)]
    )
    st = _seed(hass, steps_done=["ethernet"])

    resp = await GAOnboardingCompleteView().post(_FakeRequest(hass, {}))

    assert resp.status == 409
    assert st["completed"] is False
