"""DEVICE tests — the Danger Zone against a REAL device (canary). KB #169.

Makes the manual K0 run of 2026-07-27 repeatable. Proves, over the shipped
system, that:

* all three endpoints are master-gated and write NO marker when refused;
* the confirmation phrase is required;
* a wrong device PIN is refused **even though ``pin_verified`` is sticky-true
  on an onboarded device** — the regression this feature must never have, and
  the one thing a unit test can only assert about its own fake state;
* the backoff arms on the second wrong PIN and leaves the ONBOARDING counters
  alone;
* the soft reset removes the caller's sub-users and nothing else — rooms and
  other masters' sub-users survive.

**The accept path is deliberately NOT automated.** A valid request ends in an
irreversible tenant wipe; that belongs on a reflashable bench under a human,
not in a suite someone might point at a customer device. See the K31 record in
KB #169.

Self-cleaning: every sub-user it creates is removed again, and it refuses to
run at all if the master already owns sub-users it did not create.

    GA_DEVICE_URL=http://<device-ip>:8123 \
    GA_DEVICE_MASTER_USERNAME=<master login> \
    GA_DEVICE_MASTER_PASSWORD=<master password> \
    GA_DEVICE_PIN=<6-digit sticker PIN> \
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
DEVICE_PIN = os.environ.get("GA_DEVICE_PIN", "")

CLIENT_ID = f"{DEVICE_URL}/" if DEVICE_URL else "http://device/"
API = f"{DEVICE_URL}/api/greenautarky_site"

requires_device = pytest.mark.skipif(
    not (DEVICE_URL and MASTER_USERNAME and MASTER_PASSWORD),
    reason="GA_DEVICE_URL / GA_DEVICE_MASTER_USERNAME / GA_DEVICE_MASTER_PASSWORD not set",
)
def _require_pin() -> str:
    """The sticker PIN of the device under test, or a verdict about why not.

    A sticker PIN belongs to ONE device, but the test gate targets whichever
    canary the run picks (``testgate_device``, default K6) — the master itself
    is provisioned at run time, so nothing else here is device-bound. A single
    static PIN is therefore only valid for one target, and using it against a
    different device would not just fail: a wrong PIN increments that device's
    real backoff counters.

    So the PIN is only used when ``GA_DEVICE_PIN_FOR`` names the device we are
    actually talking to. Mismatch → skip, never guess.

    Skipping for a mismatch is fine; skipping on the MATCHING device in CI is
    not — that reports green while the one regression that would let any
    master session wipe a home goes unchecked.

    The clean fix is to stop shipping a static secret at all: the fleet-manager
    already stores ``onboarding_pin`` per device (``enrollments``), it just does
    not expose it. A scoped endpoint mirroring ``/api/devices/{id}/
    admin-credential`` would let the gate fetch the right PIN for whatever
    canary it targets — the same way it already fetches the admin credential.
    """
    target = os.environ.get("GA_DEVICE_ID", "")
    pin_for = os.environ.get("GA_DEVICE_PIN_FOR", "")

    if pin_for and target and pin_for != target:
        pytest.skip(
            f"GA_DEVICE_PIN belongs to {pin_for}, this run targets {target} — "
            "refusing to spend a wrong PIN against a real device (it would "
            "arm that device's backoff)"
        )
    if DEVICE_PIN:
        return DEVICE_PIN
    if os.environ.get("CI"):
        pytest.fail(
            f"GA_DEVICE_PIN is not set for {target or 'this device'} — the "
            "sticky-flag regression check did not run."
        )
    pytest.skip("GA_DEVICE_PIN not set (sticker PIN)")


async def _login(session, username: str, password: str) -> str:
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
        return (await resp.json())["access_token"]


async def _post(session, headers, path, body):
    async with session.post(f"{API}/{path}", headers=headers, json=body) as r:
        try:
            return r.status, await r.json()
        except Exception:
            return r.status, {}


async def _get(session, headers, path):
    async with session.get(f"{API}/{path}", headers=headers) as r:
        return r.status, await r.json()


async def _join_sub_user(session, master_h, name: str) -> tuple[str, str]:
    async with session.post(f"{API}/sub_user/invite", headers=master_h, json={}) as r:
        assert r.status == 200, await r.text()
        pin = (await r.json())["pin"]
    async with session.post(
        f"{API}/sub_user/join",
        json={"invite_pin": pin, "name": name,
              "password": f"pw-{secrets.token_urlsafe(12)}",
              "datenschutz_consent": True},
    ) as r:
        assert r.status == 200, await r.text()
        username = (await r.json())["username"]
    status, listing = await _get(session, master_h, "sub_user/list")
    assert status == 200, listing
    uid = next(u["user_id"] for u in listing["sub_users"] if u.get("name") == name)
    return uid, username


@requires_device
async def test_danger_zone_refuses_a_non_master() -> None:
    """A genuine non-master must be refused by all three endpoints, and no
    wipe may be queued as a side effect of trying."""
    aiohttp = pytest.importorskip("aiohttp")

    marker = uuid.uuid4().hex[:6]
    name = f"GateTest {marker}"
    password = f"pw-{secrets.token_urlsafe(12)}"

    async with aiohttp.ClientSession() as session:
        master_h = {"Authorization":
                    f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}
        async with session.post(f"{API}/sub_user/invite", headers=master_h,
                                json={}) as r:
            assert r.status == 200, await r.text()
            pin = (await r.json())["pin"]
        async with session.post(
            f"{API}/sub_user/join",
            json={"invite_pin": pin, "name": name, "password": password,
                  "datenschutz_consent": True},
        ) as r:
            assert r.status == 200, await r.text()
            username = (await r.json())["username"]

        _status, listing = await _get(session, master_h, "sub_user/list")
        uid = next(u["user_id"] for u in listing["sub_users"] if u.get("name") == name)
        try:
            sub_h = {"Authorization":
                     f"Bearer {await _login(session, username, password)}"}

            for path, body in (
                ("household/reset", {"confirm": "LÖSCHEN"}),
                ("site_reset/request", {"confirm": "LÖSCHEN", "pin": DEVICE_PIN or "000000"}),
            ):
                st, _ = await _post(session, sub_h, path, body)
                assert st == 403, f"{path} must be master-gated, got {st}"
            async with session.get(f"{API}/site_reset/status", headers=sub_h) as r:
                assert r.status == 403

            # …and nothing was queued behind our back.
            st, state = await _get(session, master_h, "site_reset/status")
            assert st == 200 and state["pending"] is False, state
        finally:
            await _post(session, master_h, "sub_user/remove", {"sub_user_id": uid})


@requires_device
async def test_confirmation_phrase_is_required() -> None:
    aiohttp = pytest.importorskip("aiohttp")
    async with aiohttp.ClientSession() as session:
        h = {"Authorization":
             f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}
        for body in ({"confirm": "ja bitte"}, {}):
            st, _ = await _post(session, h, "household/reset", body)
            assert st == 400, f"expected 400 for {body}, got {st}"
        st, state = await _get(session, h, "site_reset/status")
        assert state["pending"] is False


@requires_device
async def test_a_wrong_pin_is_refused_despite_the_sticky_flag() -> None:
    """The regression this feature must never have. On an onboarded device
    ``pin_verified`` is true forever; gating on it would let any master
    session wipe the home without touching the sticker."""
    _require_pin()
    aiohttp = pytest.importorskip("aiohttp")
    async with aiohttp.ClientSession() as session:
        h = {"Authorization":
             f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}

        async with session.get(f"{DEVICE_URL}/api/greenautarky_site/status") as r:
            assert (await r.json()).get("pin_verified") is True, (
                "device is not in the state this test is about — it only "
                "means something once onboarding has set the sticky flag"
            )

        st, body = await _post(session, h, "site_reset/request",
                               {"confirm": "LÖSCHEN", "pin": "000000"})
        assert st == 401, f"wrong PIN must be refused, got {st} {body}"

        st, state = await _get(session, h, "site_reset/status")
        assert state["pending"] is False, "a refused request must write no marker"


@requires_device
async def test_soft_reset_removes_only_the_callers_sub_users() -> None:
    """The whole point of the soft reset — and the one behaviour a unit test
    cannot prove about the shipped auth stack."""
    aiohttp = pytest.importorskip("aiohttp")

    marker = uuid.uuid4().hex[:6]
    async with aiohttp.ClientSession() as session:
        master_h = {"Authorization":
                    f"Bearer {await _login(session, MASTER_USERNAME, MASTER_PASSWORD)}"}

        st, listing = await _get(session, master_h, "sub_user/list")
        assert st == 200, listing
        if listing["sub_users"]:
            pytest.skip(
                "refusing to run: this master already owns "
                f"{len(listing['sub_users'])} sub-user(s) the test did not "
                "create, and the reset would delete them"
            )
        areas_before = {a["area_id"] for a in listing["areas"]}

        uid_a, _ = await _join_sub_user(session, master_h, f"ResetKid A {marker}")
        uid_b, _ = await _join_sub_user(session, master_h, f"ResetKid B {marker}")
        await _post(session, master_h, "sub_user/assign_room",
                    {"sub_user_id": uid_a, "area_id": sorted(areas_before)[0],
                     "assigned": True})
        # An invite handed out before the reset must not survive it.
        async with session.post(f"{API}/sub_user/invite", headers=master_h,
                                json={}) as r:
            assert r.status == 200

        try:
            st, body = await _post(session, master_h, "household/reset",
                                   {"confirm": "LÖSCHEN"})
            assert st == 200, body
            assert set(body["removed"]) == {uid_a, uid_b}, body

            st, after = await _get(session, master_h, "sub_user/list")
            assert st == 200
            assert after["sub_users"] == [], "sub-users survived the reset"
            assert {a["area_id"] for a in after["areas"]} == areas_before, (
                "the soft reset must not touch the home's rooms"
            )
        finally:
            for uid in (uid_a, uid_b):
                await _post(session, master_h, "sub_user/remove",
                            {"sub_user_id": uid})
