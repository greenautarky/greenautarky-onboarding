"""DEVICE tests — run the personal-dashboard use-case against a REAL device.

These hit a live GA OS device (canary) over its HA HTTP API: the full
invite → join → personal-dashboard chain, then clean up after themselves.
They are the release-gate complement to the in-process tests: same
use-case, but proven against the shipped system (component + Core + config
as actually baked).

Skipped unless the environment points at a device:

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=<master login> \
    GA_DEVICE_MASTER_PASSWORD=<master password> \
    pytest tests/device -m device

CANARIES ONLY. The master credentials must belong to a flagged master
(ga/ga-master-users.json) — the invite API is master-gated.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest

pytestmark = [pytest.mark.device, pytest.mark.asyncio]

DEVICE_URL = os.environ.get("GA_DEVICE_URL", "").rstrip("/")
MASTER_USERNAME = os.environ.get("GA_DEVICE_MASTER_USERNAME", "")
MASTER_PASSWORD = os.environ.get("GA_DEVICE_MASTER_PASSWORD", "")

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / GA_DEVICE_MASTER_USERNAME / "
    "GA_DEVICE_MASTER_PASSWORD not set",
)

CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"


async def _login(session, username: str, password: str) -> str:
    """HA login-flow → short-lived access token (no refresh needed here)."""
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    ) as resp:
        assert resp.status == 200, await resp.text()
        flow = await resp.json()
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow/{flow['flow_id']}",
        json={
            "client_id": CLIENT_ID,
            "username": username,
            "password": password,
        },
    ) as resp:
        assert resp.status == 200, await resp.text()
        result = await resp.json()
    code = result.get("result")
    assert code, f"login flow did not finish: {result}"
    async with session.post(
        f"{DEVICE_URL}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
        },
    ) as resp:
        assert resp.status == 200, await resp.text()
        token = (await resp.json())["access_token"]
    return token


@requires_device
async def test_join_yields_personal_dashboard_live() -> None:
    """Invite → join → sub_user/list shows the auto-created dashboard →
    the dashboard panel URL actually serves. Cleans up the sub-user."""
    aiohttp = pytest.importorskip("aiohttp")

    marker = uuid.uuid4().hex[:6]
    sub_name = f"PyTest Kurz {marker}"
    sub_password = f"pw-{secrets.token_urlsafe(12)}"

    async with aiohttp.ClientSession() as session:
        master_token = await _login(session, MASTER_USERNAME, MASTER_PASSWORD)
        master_h = {"Authorization": f"Bearer {master_token}"}

        # 1) master issues an invite PIN
        async with session.post(
            f"{DEVICE_URL}/api/greenautarky_onboarding/sub_user/invite",
            headers=master_h,
            json={},
        ) as resp:
            assert resp.status == 200, await resp.text()
            pin = (await resp.json())["pin"]

        sub_user_id = None
        try:
            # 2) sub-user joins (unauthenticated, PIN-gated)
            async with session.post(
                f"{DEVICE_URL}/api/greenautarky_onboarding/sub_user/join",
                json={
                    "invite_pin": pin,
                    "name": sub_name,
                    "password": sub_password,
                    "datenschutz_consent": True,
                },
            ) as resp:
                assert resp.status == 200, await resp.text()

            # 3) the master's manage surface lists the new sub-user WITH
            #    an auto-assigned ga-home-* dashboard (the feature under test)
            async with session.get(
                f"{DEVICE_URL}/api/greenautarky_onboarding/sub_user/list",
                headers=master_h,
            ) as resp:
                assert resp.status == 200, await resp.text()
                listing = await resp.json()
            sub = next(
                s for s in listing["sub_users"] if s.get("name") == sub_name
            )
            sub_user_id = sub["user_id"]
            personal = [
                d for d in (sub.get("dashboards") or []) if d.startswith("ga-home-")
            ]
            assert personal, (
                f"no auto-created ga-home-* dashboard for {sub_name}: {sub}"
            )

            # 4) the panel really serves (reachability, not just bookkeeping)
            async with session.get(
                f"{DEVICE_URL}/{personal[0]}", headers=master_h, allow_redirects=True
            ) as resp:
                assert resp.status == 200, f"panel {personal[0]} → {resp.status}"
        finally:
            # cleanup: remove the throwaway sub-user (master-gated op)
            if sub_user_id:
                await session.post(
                    f"{DEVICE_URL}/api/greenautarky_onboarding/sub_user/remove",
                    headers=master_h,
                    json={"sub_user_id": sub_user_id},
                )
