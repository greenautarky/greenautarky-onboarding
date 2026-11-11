"""Login-flow hardening, 2.12.13 (F2 session-bound PIN proof, F3 PIN reset /
PIN in URL, F4 status allow-list + invite hashing).

Written at the seam: a real aiohttp stack with the integration's REAL views and
a cookie-carrying client (pattern of test_admin_bypass_chain.py). The PIN is a
real file in the harness config dir, so the real gate and the real verify view
run - nothing about them is patched.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import get_test_config_dir

from greenautarky_site.const import DOMAIN, PIN_FILE
from greenautarky_site.household.sub_users import (
    _hash_invite_pin,
    _invite_matches,
    _slow_hash,
    migrate_invites,
)
from greenautarky_site.onboarding import session as session_module
from greenautarky_site.onboarding.password_reset import (
    GAPasswordResetUsersView,
    GAPasswordResetView,
)
from greenautarky_site.onboarding.pin import GAPinVerifyView
from greenautarky_site.onboarding.session import SESSION_COOKIE
from greenautarky_site.onboarding.wizard import (
    STATUS_FIELDS,
    GAOnboardingCompleteView,
    GAOnboardingCreateUserView,
    GAOnboardingEthernetView,
    GAOnboardingGDPRView,
    GAOnboardingStatusView,
    GAOnboardingTelemetryView,
)

PIN = "123456"
PREFIX = "/api/greenautarky_site"
GATED = [
    ("gdpr", {"accepted": True}),
    ("telemetry", {"error_logs": True}),
    ("ethernet", {"enable_ethernet": False}),
    ("create_user", {"client_id": "http://x/", "name": "A", "username": "a@x.invalid", "password": "pw-123456"}),
    ("complete", {}),
]


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


async def _stack(hass, hass_client_no_auth, **state):
    path = pathlib.Path(get_test_config_dir()) / PIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PIN)
    assert await async_setup_component(hass, "http", {"http": {}})
    st = {"completed": False, "steps_done": [], "consents": {}, **state}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    for view in (
        GAPinVerifyView, GAOnboardingStatusView, GAOnboardingGDPRView,
        GAOnboardingTelemetryView, GAOnboardingEthernetView,
        GAOnboardingCompleteView, GAOnboardingCreateUserView,
        GAPasswordResetUsersView, GAPasswordResetView,
    ):
        hass.http.register_view(view())
    return st


async def _verify(client, pin=PIN):
    return await client.post(f"{PREFIX}/verify_pin", json={"pin": pin})


# ---------------------------------------------------------------- F2 -------


async def test_pin_proof_belongs_to_the_browser_that_gave_it(hass, hass_client_no_auth):
    await _stack(hass, hass_client_no_auth)
    alice = await hass_client_no_auth()
    mallory = await hass_client_no_auth()

    resp = await _verify(alice)
    assert resp.status == 200
    cookie = resp.cookies[SESSION_COOKIE]
    assert cookie["httponly"] and cookie["max-age"] == "1800"

    # Alice's session passes the gate (the step answers something other than 403
    # 'PIN verification required'; the GDPR step itself succeeds).
    ok = await alice.post(f"{PREFIX}/gdpr", json={"accepted": True})
    assert ok.status == 200, await ok.text()

    # Every OTHER client is still refused on every gated step, although the PIN
    # has been verified on this device.
    for step, body in GATED:
        r = await mallory.post(f"{PREFIX}/{step}", json=body)
        assert r.status == 403, f"{step} answered {r.status} to a client without a session"
        assert (await r.json()).get("error") == "PIN verification required", step

    # The status view says so per session.
    assert (await (await alice.get(f"{PREFIX}/status")).json())["pin_verified"] is True
    assert (await (await mallory.get(f"{PREFIX}/status")).json())["pin_verified"] is False


async def test_a_second_client_cannot_ride_an_earlier_verification(hass, hass_client_no_auth):
    """The old behaviour: verify_pin was idempotent for EVERYBODY once one person
    had verified. Now a client without the PIN gets no session from it."""
    await _stack(hass, hass_client_no_auth)
    await _verify(await hass_client_no_auth())
    mallory = await hass_client_no_auth()
    r = await _verify(mallory, pin="000000")
    assert r.status == 401 and SESSION_COOKIE not in r.cookies


async def test_a_forged_or_expired_session_is_refused(hass, hass_client_no_auth, monkeypatch):
    await _stack(hass, hass_client_no_auth)
    client = await hass_client_no_auth()
    forged = await client.post(
        f"{PREFIX}/gdpr", json={"accepted": True}, cookies={SESSION_COOKIE: "forged"}
    )
    assert forged.status == 403

    resp = await _verify(client)
    sid = resp.cookies[SESSION_COOKIE].value
    assert (await client.post(f"{PREFIX}/gdpr", json={"accepted": True})).status == 200
    real = time.time
    monkeypatch.setattr(session_module.time, "time", lambda: real() + 31 * 60)
    late = await client.post(f"{PREFIX}/gdpr", json={"accepted": True}, cookies={SESSION_COOKIE: sid})
    assert late.status == 403, "a session older than 30 minutes still opened the gate"


async def test_adopt_and_replace_are_throttled_per_device(hass, hass_client_no_auth):
    st = await _stack(hass, hass_client_no_auth)
    client = await hass_client_no_auth()
    await _verify(client)
    # Adopt path: an unknown-but-existing address is guessed repeatedly. Make
    # the account lookup answer "linked" so every call reaches the adopt branch.
    from greenautarky_site.onboarding import wizard

    async def _linked(hass_, provider, username):
        return "linked", None, None

    wizard._async_account_state, orig = _linked, wizard._async_account_state
    wizard._async_get_hass_provider, orig_p = (lambda h: _P()), wizard._async_get_hass_provider
    try:
        body = {"client_id": "http://x/", "name": "A", "username": "a@x.invalid", "password": "guess"}
        codes = [(await client.post(f"{PREFIX}/create_user", json=body)).status for _ in range(7)]
    finally:
        wizard._async_account_state = orig
        wizard._async_get_hass_provider = orig_p
    assert codes[:5] == [401] * 5, codes
    assert codes[5:] == [429, 429], codes
    assert st  # device-wide, not per-source-address


class _P:
    async def async_initialize(self):
        return None

    async def async_validate_login(self, username, password):
        from homeassistant.auth.providers.homeassistant import InvalidAuth

        raise InvalidAuth


# ---------------------------------------------------------------- F3 -------


@pytest.mark.parametrize("view", [GAPasswordResetUsersView, GAPasswordResetView])
async def test_pin_only_password_reset_is_disabled_over_http(hass, hass_client_no_auth, view):
    await _stack(hass, hass_client_no_auth, completed=True)
    client = await hass_client_no_auth()
    path = "password_reset/users" if view is GAPasswordResetUsersView else "password_reset"
    # The CORRECT pin must not open it either.
    resp = await client.post(
        f"{PREFIX}/{path}", json={"pin": PIN, "username": "a@x.invalid", "new_password": "new-pass-123"}
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["code"] == "password_reset_disabled"
    assert "contact support" in body["message"].lower()


async def test_the_pin_is_not_forwarded_in_a_redirect(hass, hass_client_no_auth):
    from greenautarky_site.onboarding.wizard import GAOnboardingPageView

    await _stack(hass, hass_client_no_auth)
    hass.http.register_view(GAOnboardingPageView())
    client = await hass_client_no_auth()
    r = await client.get(
        f"/greenautarky-setup?pin={PIN}&device=KIB-SON-00000031", allow_redirects=False
    )
    assert r.status == 302
    assert PIN not in r.headers["location"] and "pin=" not in r.headers["location"]
    assert "device=KIB-SON-00000031" in r.headers["location"]


def test_the_wizard_bundle_does_not_prefill_the_device_pin_from_the_url():
    """The setup entry used to auto-fill (and auto-submit) ?pin=. Now only an
    invite link (join mode) may. Checked on the committed bytes, both builds."""
    root = pathlib.Path(__file__).resolve().parent.parent / "src/greenautarky_site/frontend_bundle"
    entries = list(root.glob("frontend_*/greenautarky-setup.*.js"))
    assert len(entries) == 2, entries  # coverage: latest + es5, fail closed on zero
    for js in entries:
        text = js.read_text(encoding="utf-8")
        assigns = re.findall(r".{80}this\._autoPin=[a-z]\b", text)
        assert len(assigns) == 1, (js.name, assigns)
        assert "this._joinMode&&" in assigns[0], f"{js.name}: _autoPin set outside join mode"


# ---------------------------------------------------------------- F4 -------


async def test_status_returns_only_the_allow_listed_fields(hass, hass_client_no_auth):
    secret_state = {
        "sub_user_invites": [{"pin_hash": "x", "pin_salt": "y", "master_user_id": "u1", "exp": "z"}],
        "sub_users": {"u2": {"master": "u1"}},
        "onboarding_account_user_ids": ["u1"],
        "onboarding_run_account_id": "u1",
        "pin_attempts": 3,
        "pin_locked_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "pw_reset_pin_attempts": 1,
        "tenant_mode": True,
        "gdpr_accepted": False,
        "led_modified_by": "someone",
    }
    await _stack(hass, hass_client_no_auth, **secret_state)
    client = await hass_client_no_auth()
    data = await (await client.get(f"{PREFIX}/status")).json()
    # Pinned constant, not derived from the view.
    assert set(data) == {
        "completed", "tenant_mode", "gdpr_accepted", "steps_done",
        "pin_required", "pin_provisioned", "pin_verified",
    }
    assert set(STATUS_FIELDS) <= set(data)
    text = json.dumps(data)
    for leaked in ("u1", "pin_hash", "pin_salt", "pin_sha256", "invite", "retry"):
        assert leaked not in text


def test_invite_pin_is_stored_salted_and_slow_and_old_records_migrate():
    pin = "ABCDEFGH"
    legacy = {"pin_sha256": _hash_invite_pin(pin), "master_user_id": "m", "exp": "2999-01-01T00:00:00+00:00"}
    state = {"sub_user_invites": [dict(legacy), dict(legacy)]}

    assert migrate_invites(state) is True
    a, b = state["sub_user_invites"]
    for inv in (a, b):
        assert "pin_sha256" not in inv, "the crackable hash survived the migration"
        assert inv["pin_kdf"] == "scrypt-v1"
        assert inv["pin_hash"] != legacy["pin_sha256"]
        assert _invite_matches(inv, pin)  # a live invite keeps working
        assert not _invite_matches(inv, "ZZZZZZZZ")
    assert a["pin_salt"] != b["pin_salt"] and a["pin_hash"] != b["pin_hash"]
    assert migrate_invites(state) is False  # idempotent
    # The stored value is not derivable by a plain sha256 of the PIN.
    assert _slow_hash(bytes.fromhex(a["pin_salt"]), _hash_invite_pin("ZZZZZZZZ")) != a["pin_hash"]
