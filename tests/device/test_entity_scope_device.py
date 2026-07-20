"""DEVICE tests — Stage A entity scoping against a REAL device (canary).

Proves, over the shipped system (component + Core + auth as actually baked),
that enabling entity scoping turns room scoping into a real boundary on the
state + control planes:

* a scoped sub-user's ``get_states`` collapses from "the whole house" to only
  his assigned entities (here: none, so ~0 — fail-closed);
* reading/controlling a NON-assigned entity is refused (401);
* history of that entity still returns (the documented Stage-B leak).

Self-cleaning: disables scoping and removes the throwaway sub-user.

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=<master login> \
    GA_DEVICE_MASTER_PASSWORD=<master password> \
    pytest tests/device -m device

CANARIES ONLY. Master creds must belong to a flagged master.
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
# The entity_scoping toggle is admin-only (the CI master is a plain tenant
# user). The testgate hands us the device admin credential; locally the
# master may itself be an admin — fall back to master creds then.
ADMIN_USERNAME = os.environ.get("GA_DEVICE_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("GA_DEVICE_ADMIN_PASSWORD", "")

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / GA_DEVICE_MASTER_USERNAME / GA_DEVICE_MASTER_PASSWORD not set",
)

CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"
API = f"{DEVICE_URL}/api/greenautarky_onboarding"


async def _login(session, username: str, password: str) -> str:
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow",
        json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID},
    ) as resp:
        assert resp.status == 200, await resp.text()
        flow = await resp.json()
    async with session.post(
        f"{DEVICE_URL}/auth/login_flow/{flow['flow_id']}",
        json={"client_id": CLIENT_ID, "username": username, "password": password},
    ) as resp:
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert "result" in body, f"login failed for {username!r}: {body.get('errors') or body}"
        code = body["result"]
    async with session.post(
        f"{DEVICE_URL}/auth/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
    ) as resp:
        assert resp.status == 200, await resp.text()
        return (await resp.json())["access_token"]


async def _n_states(session, headers) -> int:
    async with session.get(f"{DEVICE_URL}/api/states", headers=headers) as r:
        assert r.status == 200, await r.text()
        return len(await r.json())


@requires_device
async def test_entity_scoping_enforces_state_and_control() -> None:
    aiohttp = pytest.importorskip("aiohttp")

    marker = uuid.uuid4().hex[:6]
    sub_name = f"ScopeTest {marker}"
    sub_pw = f"pw-{secrets.token_urlsafe(12)}"

    async with aiohttp.ClientSession() as session:
        master_h = {"Authorization": f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}
        if ADMIN_USERNAME and ADMIN_PASSWORD:
            admin_h = {"Authorization": f"Bearer {await _login(session, ADMIN_USERNAME, ADMIN_PASSWORD)}"}
        else:
            admin_h = master_h

        async with session.post(f"{API}/sub_user/invite", headers=master_h, json={}) as r:
            assert r.status == 200, await r.text()
            pin = (await r.json())["pin"]

        sub_user_id = None
        scoping_was_on = False
        try:
            async with session.post(
                f"{API}/sub_user/join",
                json={"invite_pin": pin, "name": sub_name, "password": sub_pw, "datenschutz_consent": True},
            ) as r:
                assert r.status == 200, await r.text()
                # join slugifies the display name — the LOGIN username comes
                # back in the response (e.g. "ScopeTest ab12cd" -> "scopetest_ab12cd")
                sub_username = (await r.json())["username"]

            # capture the sub-user id for cleanup
            async with session.get(f"{API}/sub_user/list", headers=master_h) as r:
                assert r.status == 200, await r.text()
                sub_user_id = next(
                    s["user_id"] for s in (await r.json())["sub_users"] if s.get("name") == sub_name
                )

            sub_h = {"Authorization": f"Bearer {await _login(session, sub_username, sub_pw)}"}

            # baseline: an un-scoped sub-user sees the whole house
            base = await _n_states(session, sub_h)
            assert base > 1, f"expected a populated house, got {base} states"

            # remember prior flag so we restore it
            async with session.get(f"{API}/entity_scoping", headers=admin_h) as r:
                assert r.status == 200, await r.text()
                scoping_was_on = (await r.json())["enabled"]

            # ENABLE scoping. The sub-user has no room grant -> scoped to nothing.
            async with session.post(f"{API}/entity_scoping", headers=admin_h, json={"enabled": True}) as r:
                assert r.status == 200, await r.text()

            # a fresh token so no cached connection carries old permissions
            sub_h = {"Authorization": f"Bearer {await _login(session, sub_username, sub_pw)}"}

            scoped = await _n_states(session, sub_h)
            assert scoped < base, f"scoping did not shrink the view: {scoped} vs {base}"

            # pick a real entity the sub-user must NOT see (from the master's full view)
            async with session.get(f"{DEVICE_URL}/api/states", headers=master_h) as r:
                victim = (await r.json())[0]["entity_id"]

            # read is refused
            async with session.get(f"{DEVICE_URL}/api/states/{victim}", headers=sub_h) as r:
                assert r.status == 401, f"scoped read of {victim} was {r.status}, expected 401"
            # control is refused
            async with session.post(
                f"{DEVICE_URL}/api/services/homeassistant/update_entity",
                headers=sub_h,
                json={"entity_id": victim},
            ) as r:
                assert r.status == 401, f"scoped control of {victim} was {r.status}, expected 401"
            # history STILL leaks (documented Stage-B gap) — assert it so a future
            # Stage-B change that closes it flips this test on purpose.
            async with session.get(
                f"{DEVICE_URL}/api/history/period?filter_entity_id={victim}", headers=sub_h
            ) as r:
                assert r.status == 200, f"history of {victim} was {r.status} (Stage B changed?)"
        finally:
            # restore the flag + remove the throwaway sub-user
            await session.post(f"{API}/entity_scoping", headers=admin_h, json={"enabled": scoping_was_on})
            if sub_user_id:
                await session.post(
                    f"{API}/sub_user/remove", headers=master_h, json={"sub_user_id": sub_user_id}
                )
