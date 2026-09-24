"""The account step after a DROPPED request — the half-created account.

Measured on a canary on 2026-09-24: a create_user request that is cancelled
between the provider credential and the user link (a browser closed, a phone
locked, a Wi-Fi hop while the step runs) leaves a credential nobody owns. Every
later attempt with that address answered 400 "Could not create the account
credential": the existing-account check could not see the credential, the
create branch asked the provider for the name again, and the provider refused
it. The resident could never finish onboarding with their own address.

The adopt tests in test_onboarding_account_step.py all start from a FINISHED
first attempt (credential linked), which is why they stayed green through this.
Every test here starts from the state a dropped request actually leaves, and
reads the provider's user list directly — never through the helper under test.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.setup import async_setup_component

# The SAME placeholder the account-step suite uses, imported rather than
# repeated: a second secret-shaped literal is a second gitleaks finding to
# explain away, for a value already documented in .gitleaksignore.
from test_onboarding_account_step import THE_USUAL as SECRET

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding.wizard import GAOnboardingCreateUserView

ADDRESS = "resident@example.invalid"


# ── harness: the same recipe test_onboarding_account_step.py uses ──────────

class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}

    async def json(self) -> dict[str, Any]:
        return self._body


async def _install_hass_auth_provider(hass) -> None:
    await async_setup_component(hass, "person", {})
    await async_setup_component(hass, "http", {})
    await async_setup_component(hass, "auth", {})
    if any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        return
    from homeassistant import auth as ha_auth

    hass.auth = await ha_auth.auth_manager_from_config(hass, [{"type": "homeassistant"}], [])


def _seed(hass) -> None:
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": {
        "completed": False, "steps_done": ["pin"], "consents": {},
        "pin_verified_at": "2999-01-01T00:00:00+00:00",
    }}


async def _post(hass, secret: str = SECRET):
    return await GAOnboardingCreateUserView().post(_FakeRequest(hass, {
        "client_id": "http://localhost:8123/", "name": "Resident",
        "username": ADDRESS, "password": secret,
    }))


def _provider(hass):
    return next(p for p in hass.auth.auth_providers if p.type == "homeassistant")


async def _provider_knows(hass) -> bool:
    """Read the provider's own user list — independent of the code under test."""
    prov = _provider(hass)
    if prov.data is None:
        await prov.async_initialize()
    want = prov.data.normalize_username(ADDRESS)
    return any(prov.data.normalize_username(u["username"]) == want for u in prov.data.users)


async def _residents(hass) -> list:
    return [u for u in await hass.auth.async_get_users() if u.name == "Resident"]


async def _linked(hass) -> bool:
    creds = await _provider(hass).async_get_or_create_credentials({"username": ADDRESS})
    return await hass.auth.async_get_user_by_credentials(creds) is not None


async def _owner_exists(hass) -> None:
    """Every device has its owner before the wizard runs: converge creates
    "GreenAutarky Admin" (measured on K31). In an empty test store Home
    Assistant makes the FIRST user it creates the owner, so without this the
    leftover Resident would be the owner — a state no device is ever in."""
    await hass.auth.async_create_user("GreenAutarky Admin", group_ids=["system-admin"])


async def _dropped_state(hass) -> None:
    """Exactly what a request cancelled after the credential and before the link
    leaves: the user it created first, and a credential that nobody owns."""
    await _owner_exists(hass)
    await hass.auth.async_create_user("Resident", group_ids=[GROUP_ID_USER])
    await _provider(hass).async_add_auth(ADDRESS, SECRET)
    assert await _provider_knows(hass) and not await _linked(hass)


# ── heal (A) ───────────────────────────────────────────────────────────────

async def test_heal_01_the_same_password_finishes_a_dropped_account(hass) -> None:
    await _install_hass_auth_provider(hass)
    _seed(hass)
    await _dropped_state(hass)

    resp = await _post(hass)

    assert resp.status == 200, (
        f"answered {resp.status} — a resident whose first attempt was cut off "
        "can never finish onboarding with their own address"
    )
    assert await _linked(hass), "the credential is still owned by nobody"


async def test_heal_02_healing_reuses_the_user_the_dropped_attempt_made(hass) -> None:
    """No second 'Resident': the leftover user is the one the credential gets."""
    await _install_hass_auth_provider(hass)
    _seed(hass)
    await _dropped_state(hass)

    await _post(hass)

    residents = await _residents(hass)
    assert len(residents) == 1, f"{len(residents)} users named Resident after healing"
    assert residents[0].credentials, "the remaining Resident cannot log in"


async def test_heal_03_a_wrong_password_is_refused_and_changes_nothing(hass) -> None:
    """Healing hands out an auth_code, so it needs the password — like adopting."""
    await _install_hass_auth_provider(hass)
    _seed(hass)
    await _dropped_state(hass)

    resp = await _post(hass, secret="somebody-else-1")

    assert resp.status == 401
    assert not await _linked(hass)
    assert len(await _residents(hass)) == 1  # no new user made on the way to 401
    assert await _provider_knows(hass)


# ── prevent (B) ────────────────────────────────────────────────────────────

async def test_prevent_01_a_request_cancelled_mid_creation_leaves_no_half_account(
    hass, monkeypatch
) -> None:
    """Cancel the request exactly between the credential and the link."""
    await _install_hass_auth_provider(hass)
    _seed(hass)
    release = asyncio.Event()
    real_link = hass.auth.async_link_user

    async def slow_link(user, credentials):
        await release.wait()          # the request is cancelled while we wait here
        return await real_link(user, credentials)

    monkeypatch.setattr(hass.auth, "async_link_user", slow_link)
    task = asyncio.ensure_future(_post(hass))
    for _ in range(200):
        if await _provider_knows(hass):
            break
        await asyncio.sleep(0.01)
    assert await _provider_knows(hass), "the credential was never created"

    task.cancel()                      # the client disconnects
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if await _linked(hass):
            break

    assert await _linked(hass), (
        "the cancelled request left a credential nobody owns — the state every "
        "later attempt with this address used to fail on"
    )
    monkeypatch.setattr(hass.auth, "async_link_user", real_link)
    again = await _post(hass)          # and the retry simply adopts it
    assert again.status == 200


async def test_prevent_02_a_failed_link_removes_both_halves(hass, monkeypatch) -> None:
    """A real failure after the credential must undo the credential too."""
    await _install_hass_auth_provider(hass)
    _seed(hass)
    real_link = hass.auth.async_link_user

    async def broken_link(user, credentials):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(hass.auth, "async_link_user", broken_link)
    resp = await _post(hass)
    assert resp.status == 400
    assert not await _provider_knows(hass), (
        "the credential survived the failed attempt — the next one would meet "
        "'username_already_exists'"
    )
    assert await _residents(hass) == []

    monkeypatch.setattr(hass.auth, "async_link_user", real_link)
    again = await _post(hass)
    assert again.status == 200 and await _linked(hass)


async def test_heal_04_an_owner_is_never_handed_to_the_resident(hass) -> None:
    """If the only credential-less 'Resident' were the OWNER, reusing it would make
    the resident an administrator. Healing must make a fresh user instead."""
    await _install_hass_auth_provider(hass)
    _seed(hass)
    owner = await hass.auth.async_create_user("Resident", group_ids=[GROUP_ID_USER])
    assert owner.is_owner  # first user in an empty store: HA makes it the owner
    await _provider(hass).async_add_auth(ADDRESS, SECRET)

    resp = await _post(hass)

    assert resp.status == 200
    creds = await _provider(hass).async_get_or_create_credentials({"username": ADDRESS})
    holder = await hass.auth.async_get_user_by_credentials(creds)
    assert holder is not None and holder.id != owner.id and not holder.is_owner, (
        "the resident's credential was linked to the owner account"
    )
