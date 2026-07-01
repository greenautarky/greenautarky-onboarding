"""Tests for the sub-user (household) join foundation — ADR-0006.

Covers the security-sensitive surface: master-only invite issuing, the
one-time / TTL / backoff invite lifecycle, and that a join creates a
Non-Admin User + linked Person auto-linked to the issuing master.

The views are exercised directly (instantiate + call ``post``/``get`` with a
fake request) — the same approach HA core uses when the full HTTP stack is
overkill. ``_get_store``/``_get_state`` read ``hass.data[DOMAIN]`` which we
seed with a tiny fake store.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.setup import async_setup_component

from greenautarky_onboarding.const import DOMAIN, MASTER_USERS_FILE
from greenautarky_onboarding.http import (
    GASubUserInviteView,
    GASubUserJoinView,
    GASubUserRemoveView,
    GASubUserSetEnabledView,
    _hash_invite_pin,
    _read_master_user_ids,
    _slugify_username,
)


async def _join(hass, master, pin, name="Bob"):
    """Redeem an invite → return the created sub-user object."""
    resp = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": name, "password": "secret-pw-123", "invite_pin": pin})
    )
    assert resp.status == 200, _body(resp)
    users = await hass.auth.async_get_users()
    return next(u for u in users if u.name == name)


class _FakeStore:
    """Minimal stand-in for homeassistant.helpers.storage.Store."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    """Just enough of aiohttp.web.Request for the view methods."""

    def __init__(self, hass, body=None, hass_user=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._items: dict[str, Any] = {}
        if hass_user is not None:
            self._items["hass_user"] = hass_user

    async def json(self) -> dict[str, Any]:
        return self._body

    def __getitem__(self, key: str) -> Any:
        return self._items[key]


def _seed(hass, state: dict[str, Any] | None = None) -> tuple[_FakeStore, dict]:
    store = _FakeStore()
    st = state if state is not None else {
        "completed": True,
        "steps_done": [],
        "consents": {},
    }
    hass.data[DOMAIN] = {"store": store, "state": st}
    return store, st


def _write_master_flag(hass, *user_ids: str) -> None:
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"masters": [{"ha_user_id": u} for u in user_ids]}),
        encoding="utf-8",
    )


def _body(resp) -> dict[str, Any]:
    return json.loads(resp.body)


async def _ensure_auth_provider(hass) -> None:
    """The default test ``hass`` has no homeassistant auth provider; build one
    so credential creation (``async_add_auth``) works. Idempotent."""
    if any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        return
    from homeassistant import auth as ha_auth

    hass.auth = await ha_auth.auth_manager_from_config(
        hass, [{"type": "homeassistant"}], []
    )


async def _make_master(hass):
    """A flagged (Non-Admin) master user."""
    await _ensure_auth_provider(hass)
    master = await hass.auth.async_create_user("Master", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id)
    return master


async def _issue_invite(hass, master, ttl_hours: int | None = None) -> str:
    body = {} if ttl_hours is None else {"ttl_hours": ttl_hours}
    resp = await GASubUserInviteView().post(_FakeRequest(hass, body, hass_user=master))
    assert resp.status == 200, _body(resp)
    return _body(resp)["pin"]


# --------------------------------------------------------------------------- #
# master flag helper (fail-closed)
# --------------------------------------------------------------------------- #


def test_read_master_user_ids_missing_file(hass) -> None:
    """No flag file → empty set (fail closed, no masters)."""
    path = Path(hass.config.path(MASTER_USERS_FILE))
    if path.exists():
        path.unlink()  # the test config dir can persist across runs
    assert _read_master_user_ids(hass) == set()


def test_read_master_user_ids_malformed(hass) -> None:
    """Malformed JSON → empty set, never raises."""
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert _read_master_user_ids(hass) == set()


def test_read_master_user_ids_parses(hass) -> None:
    _write_master_flag(hass, "uuid-a", "uuid-b")
    assert _read_master_user_ids(hass) == {"uuid-a", "uuid-b"}


def test_slugify_username() -> None:
    assert _slugify_username("Anna Müller") == "anna_m_ller"
    assert _slugify_username("  ") == "user"
    assert _slugify_username("!!!") == "user"


# --------------------------------------------------------------------------- #
# invite issuing — master-only
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invite_requires_master(hass) -> None:
    _seed(hass)
    non_master = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASubUserInviteView().post(
        _FakeRequest(hass, {}, hass_user=non_master)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_invite_issued_by_master(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)
    assert len(pin) == 6 and pin.isdigit()  # 6-digit numeric (reuses the wizard PIN step)
    state = hass.data[DOMAIN]["state"]
    assert len(state["sub_user_invites"]) == 1
    # plaintext PIN must NOT be stored — only its hash
    inv = state["sub_user_invites"][0]
    assert "pin" not in inv
    assert inv["pin_sha256"] == _hash_invite_pin(pin)
    assert inv["master_user_id"] == master.id


# --------------------------------------------------------------------------- #
# join — happy path: Non-Admin user + linked Person + parent + consumed invite
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_join_creates_non_admin_user_person_and_parent(hass) -> None:
    assert await async_setup_component(hass, "person", {})
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)

    resp = await GASubUserJoinView().post(
        _FakeRequest(
            hass,
            {"name": "Anna", "password": "secret-pw-123", "invite_pin": pin},
        )
    )
    assert resp.status == 200, _body(resp)
    data = _body(resp)
    assert data["status"] == "ok"
    assert data["username"] == "anna"

    # The created user is Non-Admin.
    users = await hass.auth.async_get_users()
    anna = next(u for u in users if u.name == "Anna")
    assert not anna.is_admin
    assert any(g.id == GROUP_ID_USER for g in anna.groups)

    # Parent recorded, invite consumed.
    state = hass.data[DOMAIN]["state"]
    assert state["sub_users"][anna.id]["master"] == master.id
    assert state["sub_user_invites"] == []

    # A linked Person exists (mirror native onboarding).
    persons = [
        s for s in hass.states.async_all() if s.entity_id.startswith("person.")
    ]
    assert any(s.attributes.get("user_id") == anna.id for s in persons)


@pytest.mark.asyncio
async def test_join_username_uniquified(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    pin1 = await _issue_invite(hass, master)
    pin2 = await _issue_invite(hass, master)

    r1 = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Sam", "password": "secret-pw-123", "invite_pin": pin1})
    )
    r2 = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Sam", "password": "secret-pw-123", "invite_pin": pin2})
    )
    assert _body(r1)["username"] == "sam"
    assert _body(r2)["username"] == "sam1"


# --------------------------------------------------------------------------- #
# join — negative paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_join_short_password_rejected(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)
    resp = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Anna", "password": "short", "invite_pin": pin})
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_join_invite_is_one_time(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)
    ok = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Anna", "password": "secret-pw-123", "invite_pin": pin})
    )
    assert ok.status == 200
    # Reusing the same PIN must fail (consumed).
    again = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Other", "password": "secret-pw-123", "invite_pin": pin})
    )
    assert again.status == 401


@pytest.mark.asyncio
async def test_join_expired_invite_rejected(hass) -> None:
    master = await hass.auth.async_create_user("M", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _seed(
        hass,
        {
            "completed": True,
            "sub_user_invites": [
                {"pin_sha256": _hash_invite_pin("ABCDEFGH"),
                 "master_user_id": master.id, "exp": past},
            ],
        },
    )
    resp = await GASubUserJoinView().post(
        _FakeRequest(
            hass,
            {"name": "Anna", "password": "secret-pw-123", "invite_pin": "ABCDEFGH"},
        )
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_join_backoff_after_bad_attempts(hass) -> None:
    _seed(hass)
    await _make_master(hass)
    req = lambda: _FakeRequest(  # noqa: E731
        hass, {"name": "X", "password": "secret-pw-123", "invite_pin": "WRONGPIN"}
    )
    r1 = await GASubUserJoinView().post(req())
    assert r1.status == 401
    r2 = await GASubUserJoinView().post(req())
    assert r2.status == 401
    # After the 2nd failure a lock is armed → next call is rate-limited.
    assert hass.data[DOMAIN]["state"].get("sub_user_join_locked_until")
    r3 = await GASubUserJoinView().post(req())
    assert r3.status == 429


@pytest.mark.asyncio
async def test_join_revoked_master_rejected(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)
    # Master loses its flag before the sub-user redeems.
    _write_master_flag(hass)  # empty masters list
    resp = await GASubUserJoinView().post(
        _FakeRequest(hass, {"name": "Anna", "password": "secret-pw-123", "invite_pin": pin})
    )
    assert resp.status == 403


# --------------------------------------------------------------------------- #
# person guarantee (ADR-0006 decision: linked Person fleet-wide)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_join_guarantees_linked_person_without_preload(hass) -> None:
    """Join must create a linked Person even if 'person' was NOT set up first
    (the component loads it on demand)."""
    assert "person" not in hass.config.components
    _seed(hass)
    master = await _make_master(hass)
    pin = await _issue_invite(hass, master)
    sub = await _join(hass, master, pin, name="Bob")

    assert "person" in hass.config.components  # loaded on demand
    persons = [s for s in hass.states.async_all() if s.entity_id.startswith("person.")]
    assert any(s.attributes.get("user_id") == sub.id for s in persons)


# --------------------------------------------------------------------------- #
# lifecycle — master removes / disables own sub-users
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_remove_sub_user(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    sub = await _join(hass, master, await _issue_invite(hass, master), name="Bob")

    resp = await GASubUserRemoveView().post(
        _FakeRequest(hass, {"sub_user_id": sub.id}, hass_user=master)
    )
    assert resp.status == 200, _body(resp)
    # user gone + parent map cleared + linked person removed
    assert await hass.auth.async_get_user(sub.id) is None
    assert sub.id not in hass.data[DOMAIN]["state"].get("sub_users", {})
    persons = [s for s in hass.states.async_all() if s.entity_id.startswith("person.")]
    assert not any(s.attributes.get("user_id") == sub.id for s in persons)


@pytest.mark.asyncio
async def test_remove_rejects_foreign_sub_user(hass) -> None:
    """A master cannot remove a sub-user that isn't their own child."""
    _seed(hass)
    master = await _make_master(hass)
    sub = await _join(hass, master, await _issue_invite(hass, master), name="Bob")
    # A second, unrelated master.
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id, other.id)

    resp = await GASubUserRemoveView().post(
        _FakeRequest(hass, {"sub_user_id": sub.id}, hass_user=other)
    )
    assert resp.status == 403
    assert await hass.auth.async_get_user(sub.id) is not None  # untouched


@pytest.mark.asyncio
async def test_remove_refuses_admin(hass) -> None:
    """Refuse to delete an admin/owner even if a spoofed id lands in the map."""
    _seed(hass)
    master = await _make_master(hass)
    admin = await hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
    # Spoof: pretend the admin is this master's child.
    hass.data[DOMAIN]["state"].setdefault("sub_users", {})[admin.id] = {"master": master.id}

    resp = await GASubUserRemoveView().post(
        _FakeRequest(hass, {"sub_user_id": admin.id}, hass_user=master)
    )
    assert resp.status == 403
    assert await hass.auth.async_get_user(admin.id) is not None


@pytest.mark.asyncio
async def test_remove_requires_master(hass) -> None:
    _seed(hass)
    non_master = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASubUserRemoveView().post(
        _FakeRequest(hass, {"sub_user_id": "x"}, hass_user=non_master)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_set_enabled_toggles_login(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    sub = await _join(hass, master, await _issue_invite(hass, master), name="Bob")

    off = await GASubUserSetEnabledView().post(
        _FakeRequest(hass, {"sub_user_id": sub.id, "enabled": False}, hass_user=master)
    )
    assert off.status == 200
    assert (await hass.auth.async_get_user(sub.id)).is_active is False

    on = await GASubUserSetEnabledView().post(
        _FakeRequest(hass, {"sub_user_id": sub.id, "enabled": True}, hass_user=master)
    )
    assert on.status == 200
    assert (await hass.auth.async_get_user(sub.id)).is_active is True


@pytest.mark.asyncio
async def test_set_enabled_rejects_foreign_sub_user(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    sub = await _join(hass, master, await _issue_invite(hass, master), name="Bob")
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id, other.id)

    resp = await GASubUserSetEnabledView().post(
        _FakeRequest(hass, {"sub_user_id": sub.id, "enabled": False}, hass_user=other)
    )
    assert resp.status == 403
    assert (await hass.auth.async_get_user(sub.id)).is_active is True
