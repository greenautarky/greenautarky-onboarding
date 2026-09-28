"""Device tier — the PIN reset works on a real device (ADR-0040).

The unit tests prove the endpoints against Home Assistant's provider in a test
harness. This proves them against the device's own users, in the order the
device created them — the one thing a harness can get wrong: Home Assistant
makes the FIRST user the owner, and the owner counts as admin. If a device's
master ended up as owner, the master would be excluded from the reset and the
resident would still be locked out. Only a real device can say.

It changes a password — of a DISPOSABLE sub-user it creates and removes again.
The master's password is never touched; the master is only asserted to be
listed. Never spends a wrong PIN (that would arm the device's real backoff).

    GA_DEVICE_URL=http://<device-ip>:8123 \\
    GA_DEVICE_MASTER_USERNAME=<master login> \\
    GA_DEVICE_MASTER_PASSWORD=<master password> \\
    GA_DEVICE_PIN=<6-digit sticker PIN> GA_DEVICE_ID=<id> GA_DEVICE_PIN_FOR=<id> \\
    pytest tests/device -m device -k password_reset

CANARIES ONLY.
"""

from __future__ import annotations

import secrets
import uuid

import pytest

from tests.device.test_household_reset_device import (
    API,
    DEVICE_URL,
    MASTER_PASSWORD,
    MASTER_USERNAME,
    _get,
    _login,
    _post,
    _require_pin,
    requires_device,
)

pytestmark = [pytest.mark.device, pytest.mark.asyncio]


async def _try_login(session, username: str, password: str) -> bool:
    try:
        await _login(session, username, password)
    except AssertionError:
        return False
    return True


@requires_device
async def test_a_household_password_is_reset_with_the_sticker_pin() -> None:
    aiohttp = pytest.importorskip("aiohttp")
    pin = _require_pin()

    name = f"PwReset {uuid.uuid4().hex[:6]}"
    old_pw = f"pw-{secrets.token_urlsafe(12)}"
    new_pw = f"pw-{secrets.token_urlsafe(12)}"

    async with aiohttp.ClientSession() as session:
        master_h = {"Authorization":
                    f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}
        async with session.post(f"{API}/sub_user/invite", headers=master_h,
                                json={}) as r:
            assert r.status == 200, await r.text()
            invite = (await r.json())["pin"]
        async with session.post(
            f"{API}/sub_user/join",
            json={"invite_pin": invite, "name": name, "password": old_pw,
                  "datenschutz_consent": True},
        ) as r:
            assert r.status == 200, await r.text()
            username = (await r.json())["username"]
        _st, listing = await _get(session, master_h, "sub_user/list")
        uid = next(u["user_id"] for u in listing["sub_users"] if u.get("name") == name)

        try:
            st, body = await _post(session, {}, "password_reset/users", {"pin": pin})
            assert st == 200, body
            listed = {u["username"] for u in body["users"]}
            assert username in listed
            assert MASTER_USERNAME.strip().casefold() in {
                u.casefold() for u in listed
            }, (
                "the master is not resettable on this device — most likely it is "
                "the OWNER (created before the admin), which Home Assistant "
                "counts as admin. The resident cannot recover a forgotten password."
            )

            st, body = await _post(session, {}, "password_reset",
                                   {"pin": pin, "username": username,
                                    "new_password": new_pw})
            assert st == 200, body

            assert await _try_login(session, username, new_pw), "new password refused"
            assert not await _try_login(session, username, old_pw), (
                "the OLD password still logs in after a reset"
            )
        finally:
            await _post(session, master_h, "sub_user/remove", {"sub_user_id": uid})


@requires_device
async def test_the_reset_page_is_served() -> None:
    aiohttp = pytest.importorskip("aiohttp")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DEVICE_URL}/greenautarky-password-reset") as r:
            assert r.status == 200
            assert "Passwort zurücksetzen" in await r.text()
