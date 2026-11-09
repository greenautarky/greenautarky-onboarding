"""A PIN-verified resident may replace the not-yet-completed account (Odoo #1695).

Decided 2026-11-09: before ``complete`` no data hangs on the account, and the PIN
proves physical access, so a resident who verified the PIN in THIS run may swap
the account for one with a different address; the old user and its Person are
deleted. Everything else keeps the ``account_exists`` refusal.

The REAL views and the REAL PIN gate run (only the PIN FILE's existence is
simulated, as in test_pin_gate_fails_closed.py). Addresses are example.invalid.

On a device without a PIN file the gate answers 403 ``pin_not_provisioned`` to
every account request, replacement included: nobody can have proven physical
access there, so nothing is replaceable. Whether a device "has a PIN" is not a
choice the caller makes.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.setup import async_setup_component

from greenautarky_site.console_as import ACCOUNT_MARKER_KEY
from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module
from greenautarky_site.onboarding import wizard as wizard_module
from greenautarky_site.onboarding.wizard import (
    RUN_ACCOUNT_KEY,
    GAOnboardingCreateUserView,
)

THE_USUAL = "hunter2-abcdef"


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self._body = body
        self.cookies: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


async def _auth(hass) -> None:
    await async_setup_component(hass, "person", {})
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "auth", {})
    if not any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        from homeassistant import auth as ha_auth

        hass.auth = await ha_auth.auth_manager_from_config(
            hass, [{"type": "homeassistant"}], [])
    # A device always has the converge-created service admin, and HA makes the
    # FIRST user the owner - so the resident is never the owner on a real device.
    if not [u for u in hass.auth._store._users.values() if u.is_owner]:
        await hass.auth.async_create_user("Service", group_ids=[GROUP_ID_ADMIN])


@pytest.fixture
def run(hass, monkeypatch):
    """A device WITH a PIN file, PIN verified in this run, nothing created yet."""
    monkeypatch.setattr(pin_module, "_pin_required", lambda _h: True)
    monkeypatch.setattr(wizard_module, "_pin_required", lambda _h: True)
    st = {"completed": False, "steps_done": ["pin", "gdpr"], "consents": {}, "pin_verified": True}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


async def _post(hass, username, name, *, replace=False, secret=THE_USUAL):
    body = {"client_id": "http://localhost:8123/", "name": name, "username": username,
            "password": secret}
    if replace:
        body["replace_account"] = True
    return await GAOnboardingCreateUserView().post(_FakeRequest(hass, body))


def _users(hass, name):
    return [u for u in hass.auth._store._users.values() if u.name == name]


def _people(hass):
    from homeassistant.components import person

    return [i["name"] for i in hass.data[person.DOMAIN][1].async_items()]


async def _foreign(hass, name, username, *, admin=False):
    provider = next(p for p in hass.auth.auth_providers if p.type == "homeassistant")
    await provider.async_initialize()
    user = await hass.auth.async_create_user(
        name, group_ids=[GROUP_ID_ADMIN if admin else GROUP_ID_USER])
    await provider.async_add_auth(username, THE_USUAL)
    creds = await provider.async_get_or_create_credentials({"username": username})
    await hass.auth.async_link_user(user, creds)
    return user


async def test_replace_with_a_verified_pin_deletes_the_old_user_and_person(hass, run):
    await _auth(hass)
    assert (await _post(hass, "one@example.invalid", "One")).status == 200
    assert _people(hass) == ["One"]

    refused = await _post(hass, "two@example.invalid", "Two")
    assert refused.status == 409 and json.loads(refused.body)["replaceable"] is True

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 200 and "auth_code" in json.loads(resp.body)
    assert _users(hass, "One") == [], "the replaced user survived"
    assert len(_users(hass, "Two")) == 1
    assert _people(hass) == ["Two"], "the old Person survived or the new one is missing"
    assert run[RUN_ACCOUNT_KEY] == _users(hass, "Two")[0].id
    assert run[ACCOUNT_MARKER_KEY] == [_users(hass, "Two")[0].id]
    assert "account" in run["steps_done"]
    # The old address is free again and the new one logs in.
    provider = next(p for p in hass.auth.auth_providers if p.type == "homeassistant")
    await provider.async_validate_login("two@example.invalid", THE_USUAL)


async def test_without_the_replace_request_the_409_stays(hass, run):
    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")
    resp = await _post(hass, "two@example.invalid", "Two")
    assert resp.status == 409 and json.loads(resp.body)["code"] == "account_exists"
    assert len(_users(hass, "One")) == 1 and _users(hass, "Two") == []


async def test_without_a_pin_verified_in_this_run_nothing_is_replaced(hass, run):
    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")
    run["pin_verified"] = False  # what a reset does

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 403
    assert len(_users(hass, "One")) == 1 and _users(hass, "Two") == []


async def test_on_a_device_without_a_pin_file_nothing_is_replaced(hass, run, monkeypatch):
    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")
    monkeypatch.setattr(pin_module, "_pin_required", lambda _h: False)
    monkeypatch.setattr(wizard_module, "_pin_required", lambda _h: False)

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 403 and json.loads(resp.body)["code"] == "pin_not_provisioned"
    assert len(_users(hass, "One")) == 1 and _users(hass, "Two") == []


async def test_after_completion_there_is_no_replacement(hass, run):
    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")
    run["completed"] = True

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 403
    assert len(_users(hass, "One")) == 1 and _users(hass, "Two") == []


async def test_an_admin_the_wizard_recorded_is_never_deleted(hass, run):
    await _auth(hass)
    victim = await _foreign(hass, "Victim", "victim@example.invalid", admin=True)
    run["steps_done"].append("account")
    run[RUN_ACCOUNT_KEY] = victim.id
    run[ACCOUNT_MARKER_KEY] = [victim.id]

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 409 and json.loads(resp.body)["replaceable"] is False
    assert len(_users(hass, "Victim")) == 1 and _users(hass, "Two") == []


@pytest.mark.parametrize("tracked", ["bystander", "nothing"])
async def test_a_user_the_wizard_did_not_create_is_never_deleted(hass, run, tracked):
    """The marker names one user, the tracked id names another (or none): no deletion."""
    await _auth(hass)
    marked = await _foreign(hass, "Marked", "marked@example.invalid")
    bystander = await _foreign(hass, "Bystander", "bystander@example.invalid")
    run["steps_done"].append("account")
    run[ACCOUNT_MARKER_KEY] = [marked.id]
    if tracked == "bystander":
        run[RUN_ACCOUNT_KEY] = bystander.id

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 409 and json.loads(resp.body)["replaceable"] is False
    for name in ("Marked", "Bystander"):
        assert len(_users(hass, name)) == 1
    assert _users(hass, "Two") == []


async def test_an_adopted_account_is_not_replaceable(hass, run):
    """Adopting an existing user (right password) does not make it the wizard's to delete."""
    await _auth(hass)
    await _foreign(hass, "Existing", "existing@example.invalid")
    assert (await _post(hass, "existing@example.invalid", "Existing")).status == 200
    assert RUN_ACCOUNT_KEY not in run

    resp = await _post(hass, "two@example.invalid", "Two", replace=True)

    assert resp.status == 409 and json.loads(resp.body)["replaceable"] is False
    assert len(_users(hass, "Existing")) == 1


async def test_the_same_address_is_still_adopted(hass, run):
    """Must-pass: the idempotent retry is unchanged, with or without the flag."""
    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")
    uid = run[RUN_ACCOUNT_KEY]

    for replace in (False, True):
        resp = await _post(hass, "one@example.invalid", "One", replace=replace)
        assert resp.status == 200
    assert len(_users(hass, "One")) == 1 and run[RUN_ACCOUNT_KEY] == uid
    assert _people(hass) == ["One"]


async def test_a_reset_forgets_the_tracked_account(hass, run):
    from greenautarky_site.onboarding.wizard import GAOnboardingResetView

    await _auth(hass)
    await _post(hass, "one@example.invalid", "One")

    class _U:
        is_admin = True
        name = "qa"

    class _Req(dict):
        def __init__(self) -> None:
            super().__init__(hass_user=_U())
            self.app = {"hass": hass}

    req = _Req()
    from homeassistant.components.frontend import DATA_PANELS

    from greenautarky_site import PANEL_URL_PATH

    hass.data.setdefault(DATA_PANELS, {})[PANEL_URL_PATH] = object()
    resp = await GAOnboardingResetView().post(req)

    assert resp.status == 200 and RUN_ACCOUNT_KEY not in run
