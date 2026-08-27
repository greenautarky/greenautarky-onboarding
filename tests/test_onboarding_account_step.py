"""The account step must survive being walked twice.

THE DEFECT THIS FILE EXISTS FOR, measured on a bench device on 2026-08-27.

``GAOnboardingCreateUserView`` created the Home Assistant user FIRST and added
the credential second. A username that already existed raised ``InvalidUser``,
the handler returned 400 — and the user it had just created stayed behind, with
no credential and invisible to the wizard. The panel offers no way past the
account step, so the only thing a resident can do is press the button again,
and every press left another orphan.

Five attempts produced THIRTEEN users named "resident", exactly one of which
could log in. The device sat at ``completed: false`` with "account" already in
``steps_done`` — set up, and locked out. A dropped connection while submitting
is enough to reach that state; nobody has to do anything wrong.

Two failures, asserted separately because they are different problems:

  the ORPHAN     a retry must leave exactly one user, not two
  the DEAD END   a retry must not answer 400, because there is no other way on

Written to the pattern the rest of this suite uses: the view is called
directly with a fake request, rather than standing the whole integration up.
The first version of this file used ``async_setup_component`` and failed with
"Integration not found" — red, and red for a reason that says nothing about
the defect, which is not a red proof at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding.wizard import GAOnboardingCreateUserView


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._items: dict[str, Any] = {}

    async def json(self) -> dict[str, Any]:
        return self._body

    def __getitem__(self, key: str) -> Any:
        return self._items[key]


def _seed(hass) -> dict:
    """An onboarding in progress, with the PIN already verified.

    The account step is gated on both, so without this the view answers before
    reaching the code under test.
    """
    st = {
        "completed": False,
        "steps_done": ["pin"],
        "consents": {},
        "pin_verified_at": "2999-01-01T00:00:00+00:00",
    }
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


def _count(hass, name: str) -> int:
    return len([u for u in hass.auth._store._users.values() if u.name == name])


async def _post(hass, username: str, name: str = "Resident"):
    return await GAOnboardingCreateUserView().post(
        _FakeRequest(
            hass,
            {
                "client_id": "http://localhost:8123/",
                "name": name,
                "username": username,
                "password": "hunter2-abcdef",
            },
        )
    )


@pytest.mark.asyncio
async def test_creating_the_account_twice_leaves_exactly_one_user(hass) -> None:
    """The orphan half.

    Before the fix this left TWO users named Resident: the second attempt
    created one, failed to add the credential, and returned 400 without
    removing it. Thirteen accumulated on a real device.
    """
    _seed(hass)
    await _post(hass, "resident@example.invalid")
    assert _count(hass, "Resident") == 1

    await _post(hass, "resident@example.invalid")
    assert _count(hass, "Resident") == 1, (
        f"a repeated account step left {_count(hass, 'Resident')} users named "
        "Resident — every retry orphans another one"
    )


@pytest.mark.asyncio
async def test_the_account_step_is_not_a_dead_end(hass) -> None:
    """The lock-out half.

    Before the fix the second call answered 400 "Username already exists" and
    the panel had nothing else to offer, so onboarding could never complete on
    a device whose account step had half-succeeded.
    """
    _seed(hass)
    await _post(hass, "twice@example.invalid")
    again = await _post(hass, "twice@example.invalid")

    assert again.status != 400, (
        "the account step answered 400 on a retry. The wizard offers no way "
        "past this step, so a resident whose first attempt half-succeeded — a "
        "dropped connection is enough — can never finish onboarding."
    )


@pytest.mark.asyncio
async def test_a_different_username_still_creates_a_second_account(hass) -> None:
    """The must-pass half.

    A fix that simply refused to create anything would satisfy both assertions
    above and break onboarding entirely. Adopting an existing account must not
    become never creating one.
    """
    _seed(hass)
    await _post(hass, "one@example.invalid", name="One")
    await _post(hass, "two@example.invalid", name="Two")

    assert _count(hass, "One") == 1
    assert _count(hass, "Two") == 1
