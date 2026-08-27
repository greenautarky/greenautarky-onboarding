"""The account step must survive being walked twice.

THE DEFECT THIS FILE EXISTS FOR, measured on a bench device on 2026-08-27.

`GACreateUserView` created the Home Assistant user FIRST and added the
credential second. A username that already existed raised `InvalidUser`, the
handler returned 400 — and the user it had just created stayed behind, with no
credential and invisible to the wizard. The panel offers no way past the
account step, so the only thing a resident can do is press the button again,
and every press left another orphan.

Five attempts produced THIRTEEN users named "resident", exactly one of which
could log in. The device sat at `completed: false` with "account" already in
`steps_done` — set up, and locked out. A dropped connection while submitting
is enough to reach that state; it does not need anyone to do anything wrong.

Two separate failures, asserted separately:

  * a retry must not LEAK users   (the orphan)
  * a retry must not FAIL         (the dead end)
"""

from __future__ import annotations

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.setup import async_setup_component

from greenautarky_site.const import DOMAIN


def _names(hass, name: str) -> int:
    return len([u for u in hass.auth._store._users.values() if u.name == name])


async def _post_account(hass, client, username: str, password: str, name: str = "Resident"):
    return await client.post(
        "/api/greenautarky_site/create_user",
        json={
            "client_id": "http://localhost:8123/",
            "name": name,
            "username": username,
            "password": password,
        },
    )


@pytest.mark.asyncio
async def test_creating_the_account_twice_leaves_exactly_one_user(hass, hass_client_no_auth):
    """The orphan half of the defect.

    Before the fix this left TWO users named Resident: the second attempt
    created one, failed to add the credential, and returned 400 without
    removing it. Thirteen of them accumulated on a real device.
    """
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    client = await hass_client_no_auth()

    first = await _post_account(hass, client, "resident@example.invalid", "hunter2-abc")
    assert first.status in (200, 201), await first.text()
    assert _names(hass, "Resident") == 1

    second = await _post_account(hass, client, "resident@example.invalid", "hunter2-abc")
    # The count is the assertion that matters. Whatever the status, a retry
    # must never multiply accounts.
    assert _names(hass, "Resident") == 1, (
        f"a repeated account step left {_names(hass, 'Resident')} users named "
        "Resident — every retry orphans another one"
    )
    assert second.status in (200, 201, 409), await second.text()


@pytest.mark.asyncio
async def test_the_account_step_is_not_a_dead_end(hass, hass_client_no_auth):
    """The lock-out half.

    Before the fix the second call answered 400 "Username already exists" and
    the panel had nothing else to offer, so onboarding could never complete on
    a device whose account step had half-succeeded.
    """
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    client = await hass_client_no_auth()

    await _post_account(hass, client, "twice@example.invalid", "hunter2-abc")
    again = await _post_account(hass, client, "twice@example.invalid", "hunter2-abc")

    assert again.status != 400, (
        "the account step answered 400 on a retry. The wizard offers no way "
        "past this step, so a resident whose first attempt half-succeeded — a "
        "dropped connection is enough — can never finish onboarding."
    )


@pytest.mark.asyncio
async def test_a_different_username_still_creates_a_second_account(hass, hass_client_no_auth):
    """The must-pass half.

    A fix that simply refused to create anything would satisfy both tests
    above and break onboarding entirely. Adopting an existing account must not
    turn into never creating one.
    """
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    client = await hass_client_no_auth()

    await _post_account(hass, client, "one@example.invalid", "hunter2-abc", name="One")
    await _post_account(hass, client, "two@example.invalid", "hunter2-abc", name="Two")

    assert _names(hass, "One") == 1
    assert _names(hass, "Two") == 1
