"""Device tier — the PIN reset works on a real device (ADR-0040).

The unit tests prove the endpoints against Home Assistant's provider in a test
harness. This proves them against the device's own users, in the order the
device created them — the one thing a harness can get wrong: Home Assistant
makes the FIRST user the owner, and the owner counts as admin. If a device's
master ended up as owner, the master would be excluded from the reset and the
resident would still be locked out. Only a real device can say.

It also proves D6: a session opened with the sub-user's OLD password no longer
refreshes after the reset, while the master's session does.

It changes a password — of a DISPOSABLE sub-user it creates and removes again.
The master's password is never touched; the master is only asserted to be
listed. Never spends a wrong PIN (that would arm the device's real backoff).

    GA_DEVICE_URL=http://<device-ip>:<port> \\
    GA_DEVICE_MASTER_USERNAME=<master login> \\
    GA_DEVICE_MASTER_PASSWORD=<master password> \\
    GA_DEVICE_PIN=<6-digit sticker PIN> GA_DEVICE_ID=<id> GA_DEVICE_PIN_FOR=<id> \\
    pytest tests/device -m device -k password_reset

CANARIES ONLY.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest

pytestmark = [pytest.mark.device, pytest.mark.asyncio]

# Origin + client_id come from one place: Core refuses a mesh-IP client_id
# without a port, and the browser drops :80 from hassUrl (device_target.py).
from device_target import CLIENT_ID, DEVICE_URL  # noqa: E402

MASTER_USERNAME = os.environ.get("GA_DEVICE_MASTER_USERNAME", "")
MASTER_PASSWORD = os.environ.get("GA_DEVICE_MASTER_PASSWORD", "")
DEVICE_PIN = os.environ.get("GA_DEVICE_PIN", "")

API = f"{DEVICE_URL}/api/greenautarky_site"

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / GA_DEVICE_MASTER_USERNAME / GA_DEVICE_MASTER_PASSWORD not set",
)

# The helpers below are copied from test_household_reset_device.py, not
# imported: CI runs `pytest` (not `python -m pytest`), so `tests` is not an
# importable package there and a cross-file import breaks collection of the
# whole unit suite.


def _require_pin() -> str:
    """The sticker PIN, only for the device it belongs to (see
    test_household_reset_device._require_pin for why a mismatch must skip)."""
    target = os.environ.get("GA_DEVICE_ID", "")
    pin_for = os.environ.get("GA_DEVICE_PIN_FOR", "")
    if pin_for and target and pin_for != target:
        pytest.skip(
            f"GA_DEVICE_PIN belongs to {pin_for}, this run targets {target} — "
            "refusing to spend a wrong PIN against a real device"
        )
    if DEVICE_PIN:
        return DEVICE_PIN
    if os.environ.get("CI"):
        pytest.fail(f"GA_DEVICE_PIN is not set for {target or 'this device'}")
    pytest.skip("GA_DEVICE_PIN not set (sticker PIN)")


async def _login(session, username: str, password: str) -> str:
    return (await _login_tokens(session, username, password))["access_token"]


async def _login_tokens(session, username: str, password: str) -> dict:
    """The full /auth/token answer: access_token AND refresh_token."""
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow",
        json={"client_id": CLIENT_ID, "handler": ["homeassistant", None],
              "redirect_uri": CLIENT_ID},
    ) as resp:
        assert resp.status == 200, await resp.text()
        flow = await resp.json()
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow/{flow['flow_id']}",
        json={"client_id": CLIENT_ID, "username": username, "password": password},
    ) as resp:
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert "result" in body, f"login failed for {username!r}: {body.get('errors')}"
        code = body["result"]
    async with session.post(
        f"{DEVICE_URL}/auth/token",
        data={"grant_type": "authorization_code", "code": code,
              "client_id": CLIENT_ID},
    ) as resp:
        assert resp.status == 200, await resp.text()
        return await resp.json()


async def _refresh(session, refresh_token: str) -> int:
    """HTTP status of a refresh_token grant — 200 while the session lives."""
    async with session.post(
        f"{DEVICE_URL}/auth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": CLIENT_ID},
    ) as resp:
        return resp.status


async def _post(session, headers, path, body):
    async with session.post(f"{API}/{path}", headers=headers, json=body) as r:
        try:
            return r.status, await r.json()
        except Exception:
            return r.status, {}


async def _get(session, headers, path):
    async with session.get(f"{API}/{path}", headers=headers) as r:
        return r.status, await r.json()


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
        master_tokens = await _login_tokens(session, MASTER_USERNAME, MASTER_PASSWORD)
        master_refresh = master_tokens["refresh_token"]
        master_h = {"Authorization": f"Bearer {master_tokens['access_token']}"}
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
            # A session opened with the OLD password — e.g. a phone still
            # logged in. ADR-0040 D6: the reset must end it.
            old_session = (await _login_tokens(session, username, old_pw))[
                "refresh_token"
            ]
            assert await _refresh(session, old_session) == 200, (
                "precondition: the old-password session refreshes before the reset"
            )

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
            assert await _refresh(session, old_session) != 200, (
                "the session opened with the OLD password still refreshes after "
                "the reset — the reset did not end the user's sessions (D6)"
            )
            assert await _refresh(session, master_refresh) == 200, (
                "resetting a sub-user ended the MASTER's session"
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
