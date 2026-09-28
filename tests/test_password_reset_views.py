"""The PIN-gated password reset — the endpoints behind the login page's link.

``onboarding/password_reset.py`` shipped with no test at all. ADR-0040 makes
it the resident's only way back in after a forgotten password, so its three
promises get pinned here, against Home Assistant's REAL auth provider:

1. the sticker PIN is required, and a wrong one arms the backoff;
2. a household user — the master included, which the wizard creates as
   GROUP_ID_USER — can be reset, and afterwards the NEW password logs in and
   the old one does not;
3. an admin account is never listed and never reset.

The PIN is a real file in the harness config dir (cleaned by conftest): the
module imports ``_pin_required`` by name, so patching ``pin._pin_required``
would leave the gate under test untouched and prove nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.providers.homeassistant import InvalidAuth
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import get_test_config_dir

from greenautarky_site.const import DOMAIN, PIN_FILE
from greenautarky_site.onboarding.password_reset import (
    GAPasswordResetUsersView,
    GAPasswordResetView,
)

PIN = "123456"
OLD_PW = "old-password-1"
NEW_PW = "new-password-2"


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body: dict[str, Any]) -> None:
        self.app = {"hass": hass}
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


def _write_pin() -> None:
    path = Path(get_test_config_dir()) / PIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PIN)


async def _provider(hass):
    """Home Assistant's own username/password provider (recipe from
    test_onboarding_account_step.py, which documents why each line is there)."""
    await async_setup_component(hass, "person", {})
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "auth", {})
    if not any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        from homeassistant import auth as ha_auth

        hass.auth = await ha_auth.auth_manager_from_config(
            hass, [{"type": "homeassistant"}], []
        )
    provider = next(p for p in hass.auth.auth_providers if p.type == "homeassistant")
    await provider.async_initialize()
    return provider


async def _user(hass, provider, name: str, username: str, group: str):
    user = await hass.auth.async_create_user(name, group_ids=[group])
    await provider.async_add_auth(username, OLD_PW)
    cred = await provider.async_get_or_create_credentials({"username": username})
    await hass.auth.async_link_user(user, cred)
    return user


@pytest.fixture
async def household(hass):
    """An admin, then a master (GROUP_ID_USER, as the wizard creates it).

    Order matters and mirrors the device: Home Assistant makes the FIRST user
    the owner, and ``is_admin`` is true for the owner whatever its group. On a
    GA device provisioning creates the admin before the wizard runs, so the
    master is never the owner — created the other way round, the master would
    be excluded as an admin and these tests would test the wrong household.
    """
    _write_pin()
    provider = await _provider(hass)
    await _user(hass, provider, "Admin", "ga-admin", GROUP_ID_ADMIN)
    master = await _user(hass, provider, "Anna", "anna@example.org", GROUP_ID_USER)
    assert not master.is_owner and not master.is_admin  # precondition, see above
    state: dict[str, Any] = {"completed": True}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": state}
    return provider, state


async def _users(hass, pin: str = PIN):
    return await GAPasswordResetUsersView().post(_FakeRequest(hass, {"pin": pin}))


async def _reset(hass, username: str, pin: str = PIN, password: str = NEW_PW):
    return await GAPasswordResetView().post(
        _FakeRequest(hass, {"pin": pin, "username": username, "new_password": password})
    )


def _json(resp) -> Any:
    import json

    return json.loads(resp.body)


async def test_the_master_is_listed_and_the_admin_is_not(hass, household):
    resp = await _users(hass)
    assert resp.status == 200
    usernames = {u["username"] for u in _json(resp)["users"]}
    assert "anna@example.org" in usernames, (
        "the master is GROUP_ID_USER — if it is missing here, the resident who "
        "owns the device cannot reset their own password"
    )
    assert "ga-admin" not in usernames


async def test_the_master_resets_and_the_new_password_logs_in(hass, household):
    provider, _state = household
    resp = await _reset(hass, "anna@example.org")
    assert resp.status == 200, resp.body

    await provider.async_validate_login("anna@example.org", NEW_PW)
    with pytest.raises(InvalidAuth):
        await provider.async_validate_login("anna@example.org", OLD_PW)


async def test_the_admin_is_refused_and_keeps_its_password(hass, household):
    provider, _state = household
    resp = await _reset(hass, "ga-admin")
    assert resp.status == 404

    await provider.async_validate_login("ga-admin", OLD_PW)


@pytest.mark.parametrize("call", ["users", "reset"])
async def test_a_wrong_pin_changes_nothing_and_counts(hass, household, call):
    provider, state = household
    resp = await (_users(hass, "654321") if call == "users"
                  else _reset(hass, "anna@example.org", pin="654321"))
    assert resp.status == 401
    assert state["pw_reset_pin_attempts"] == 1
    await provider.async_validate_login("anna@example.org", OLD_PW)


async def test_the_second_wrong_pin_locks_even_the_right_one(hass, household):
    """Backoff: after two misses the right PIN is refused too, until the lock
    runs out — otherwise the six digits are a counter, not a gate."""
    _, state = household
    assert (await _users(hass, "000000")).status == 401
    assert (await _users(hass, "000001")).status == 401
    assert state.get("pw_reset_pin_locked_until")

    assert (await _users(hass)).status == 429
    assert (await _reset(hass, "anna@example.org")).status == 429


async def test_a_success_clears_the_counter(hass, household):
    _, state = household
    assert (await _reset(hass, "anna@example.org", pin="999999")).status == 401
    assert (await _reset(hass, "anna@example.org")).status == 200
    assert state["pw_reset_pin_attempts"] == 0


async def test_no_pin_file_means_no_reset(hass, household):
    """A device without a PIN has no reset — which is why the login link keeps
    Home Assistant's target there (see test_forgot_password_link.py)."""
    (Path(get_test_config_dir()) / PIN_FILE).unlink()
    assert (await _users(hass)).status == 404
    assert (await _reset(hass, "anna@example.org")).status == 404


async def test_the_pin_accepts_the_label_format(hass, household):
    """The label prints ``123-456``; the page sends what the resident typed."""
    assert (await _users(hass, "123-456")).status == 200


async def _by_name(hass, name: str):
    return next(u for u in await hass.auth.async_get_users() if u.name == name)


async def test_a_reset_ends_that_users_sessions_and_only_theirs(hass, household):
    """ADR-0040 D6: a device still logged in with the old password is logged
    out. The admin's session — a different user — must survive the reset."""
    master = await _by_name(hass, "Anna")
    admin = await _by_name(hass, "Admin")
    client = "http://device.test/"
    old_session = await hass.auth.async_create_refresh_token(master, client)
    old_access = hass.auth.async_create_access_token(old_session)
    admin_session = await hass.auth.async_create_refresh_token(admin, client)
    assert hass.auth.async_get_refresh_token(old_session.id) is not None

    assert (await _reset(hass, "anna@example.org")).status == 200

    assert hass.auth.async_get_refresh_token(old_session.id) is None, (
        "the session opened with the OLD password still refreshes after a reset"
    )
    assert hass.auth.async_validate_access_token(old_access) is None
    assert not (await _by_name(hass, "Anna")).refresh_tokens
    assert hass.auth.async_get_refresh_token(admin_session.id) is not None, (
        "a reset of the master ended the admin's session"
    )
