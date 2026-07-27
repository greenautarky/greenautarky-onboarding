"""Tests for the resident self-service reset — KB #169.

Two actions with very different blast radius, so the tests are written around
what must SURVIVE as much as what must go:

  * household reset — every sub-user of the calling master disappears, and the
    master, the rooms and the areas do not.
  * site reset — the marker file is the entire contract with ga_manager, so its
    shape, its freshness and the gates in front of it are what get asserted.

The sticky-flag regression is called out explicitly: ``pin_verified`` stays
true forever once onboarding is done, so a destructive action gated on it
would wave through any master session. ``async_verify_pin_fresh`` must re-read
the file every time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_USER

from greenautarky_site.const import (
    DOMAIN,
    MASTER_USERS_FILE,
    PIN_FILE,
    RESET_REQUEST_FILE,
    RESET_REQUEST_SCHEMA_VERSION,
    RESET_STATUS_FILE,
)
from greenautarky_site.household import (
    GAHouseholdResetView,
    GASiteResetRequestView,
    GASiteResetStatusView,
    GASubUserInviteView,
    GASubUserJoinView,
)
from greenautarky_site.scoping import rooms

DEVICE_PIN = "123456"


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
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


@pytest.fixture(autouse=True)
def _clean_reset_markers(hass):
    """The hass config dir is shared between tests in a module, so a marker
    written by one test would make the next one see a phantom pending wipe.
    Clear both files before and after every test."""

    def _clear() -> None:
        for name in (RESET_REQUEST_FILE, RESET_STATUS_FILE):
            Path(hass.config.path(name)).unlink(missing_ok=True)

    _clear()
    yield
    _clear()


def _seed(hass, state: dict[str, Any] | None = None) -> dict:
    st = state if state is not None else {"completed": True}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


def _write_master_flag(hass, *user_ids: str) -> None:
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"masters": [{"ha_user_id": u} for u in user_ids]}), encoding="utf-8"
    )


def _write_device_pin(hass, pin: str = DEVICE_PIN) -> None:
    path = Path(hass.config.path(PIN_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pin, encoding="utf-8")


def _body(resp) -> dict[str, Any]:
    return json.loads(resp.body)


async def _ensure_auth_provider(hass) -> None:
    if any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        return
    from homeassistant import auth as ha_auth

    hass.auth = await ha_auth.auth_manager_from_config(
        hass, [{"type": "homeassistant"}], []
    )


async def _make_master(hass):
    await _ensure_auth_provider(hass)
    master = await hass.auth.async_create_user("Master", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id)
    return master


async def _join_sub_user(hass, master, name="Kid"):
    inv = await GASubUserInviteView().post(_FakeRequest(hass, {}, hass_user=master))
    pin = _body(inv)["pin"]
    resp = await GASubUserJoinView().post(
        _FakeRequest(
            hass,
            {
                "name": name,
                "password": "secret-pw-123",
                "invite_pin": pin,
                "datenschutz_consent": True,
            },
        )
    )
    assert resp.status == 200, _body(resp)
    return next(u for u in await hass.auth.async_get_users() if u.name == name)


# --------------------------------------------------------------------------- #
# household reset (soft)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_household_reset_requires_master(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    stranger = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GAHouseholdResetView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN"}, hass_user=stranger)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_household_reset_requires_confirm_phrase(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    resp = await GAHouseholdResetView().post(
        _FakeRequest(hass, {"confirm": "ok"}, hass_user=master)
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_household_reset_removes_sub_users_and_keeps_the_home(hass) -> None:
    state = _seed(hass)
    master = await _make_master(hass)
    kid = await _join_sub_user(hass, master, "Kid")
    flatmate = await _join_sub_user(hass, master, "Flatmate")

    # Grant a room so we can prove the grant dies with the user.
    from homeassistant.helpers import area_registry as ar

    area = ar.async_get(hass).async_create("Kitchen")
    rooms.room_matrix(state)[kid.id] = [area.id]
    # An invite handed out yesterday must not survive the reset.
    assert state.get("sub_user_invites") is not None
    inv = await GASubUserInviteView().post(_FakeRequest(hass, {}, hass_user=master))
    assert _body(inv)["pin"]

    resp = await GAHouseholdResetView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN"}, hass_user=master)
    )
    assert resp.status == 200, _body(resp)
    assert set(_body(resp)["removed"]) == {kid.id, flatmate.id}

    # Gone: accounts + every bookkeeping dict.
    remaining = {u.id for u in await hass.auth.async_get_users()}
    assert kid.id not in remaining
    assert flatmate.id not in remaining
    assert state.get("sub_users") == {}
    assert kid.id not in (state.get(rooms.STATE_ROOMS) or {})
    assert state.get("sub_user_dashboards", {}) == {}
    assert state["sub_user_invites"] == []

    # Survived: the master and the home itself.
    assert master.id in remaining
    assert ar.async_get(hass).async_get_area(area.id) is not None


@pytest.mark.asyncio
async def test_household_reset_is_idempotent_on_an_empty_household(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    resp = await GAHouseholdResetView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN"}, hass_user=master)
    )
    assert resp.status == 200
    assert _body(resp)["removed"] == []


@pytest.mark.asyncio
async def test_household_reset_spares_another_masters_sub_users(hass) -> None:
    state = _seed(hass)
    master = await _make_master(hass)
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id, other.id)
    mine = await _join_sub_user(hass, master, "Mine")
    theirs = await _join_sub_user(hass, other, "Theirs")

    resp = await GAHouseholdResetView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN"}, hass_user=master)
    )
    assert resp.status == 200
    assert _body(resp)["removed"] == [mine.id]
    assert theirs.id in (state.get("sub_users") or {})


# --------------------------------------------------------------------------- #
# site reset (hard) — the marker contract
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_site_reset_requires_master(hass) -> None:
    _seed(hass)
    _write_device_pin(hass)
    await _ensure_auth_provider(hass)
    stranger = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASiteResetRequestView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN", "pin": DEVICE_PIN}, hass_user=stranger)
    )
    assert resp.status == 403
    assert not Path(hass.config.path(RESET_REQUEST_FILE)).exists()


@pytest.mark.asyncio
async def test_site_reset_requires_confirm_phrase(hass) -> None:
    _seed(hass)
    _write_device_pin(hass)
    master = await _make_master(hass)
    resp = await GASiteResetRequestView().post(
        _FakeRequest(hass, {"confirm": "yes", "pin": DEVICE_PIN}, hass_user=master)
    )
    assert resp.status == 400
    assert not Path(hass.config.path(RESET_REQUEST_FILE)).exists()


@pytest.mark.asyncio
async def test_site_reset_rejects_a_wrong_pin_despite_the_sticky_flag(hass) -> None:
    """The regression this feature must never ship: ``pin_verified`` is sticky
    after onboarding, so gating on it would let any master session wipe the
    device without touching the sticker."""
    _seed(hass, {"completed": True, "pin_verified": True})
    _write_device_pin(hass)
    master = await _make_master(hass)

    resp = await GASiteResetRequestView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN", "pin": "000000"}, hass_user=master)
    )
    assert resp.status == 401
    assert not Path(hass.config.path(RESET_REQUEST_FILE)).exists()


@pytest.mark.asyncio
async def test_site_reset_pin_backoff_locks_out(hass) -> None:
    state = _seed(hass, {"completed": True})
    _write_device_pin(hass)
    master = await _make_master(hass)

    for _ in range(2):
        resp = await GASiteResetRequestView().post(
            _FakeRequest(hass, {"confirm": "LÖSCHEN", "pin": "000000"}, hass_user=master)
        )
        assert resp.status == 401

    # Second failure arms the backoff; the next attempt is refused outright —
    # even the correct PIN has to wait.
    resp = await GASiteResetRequestView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN", "pin": DEVICE_PIN}, hass_user=master)
    )
    assert resp.status == 429
    assert state["site_reset_pin_attempts"] == 2
    # The onboarding counters are untouched — a fumbled reset must not lock a
    # tenant out of their own wizard.
    assert "pin_attempts" not in state


@pytest.mark.asyncio
async def test_site_reset_writes_a_fresh_valid_marker(hass) -> None:
    _seed(hass)
    _write_device_pin(hass)
    master = await _make_master(hass)

    resp = await GASiteResetRequestView().post(
        _FakeRequest(
            hass,
            {"confirm": "löschen", "pin": "123-456"},  # case + dashes tolerated
            hass_user=master,
        )
    )
    assert resp.status == 202, _body(resp)

    marker = json.loads(Path(hass.config.path(RESET_REQUEST_FILE)).read_text())
    assert marker["schema_version"] == RESET_REQUEST_SCHEMA_VERSION
    assert marker["kind"] == "tenant-wipe"
    assert marker["nonce"] == _body(resp)["nonce"]
    assert marker["requested_by"]["ha_user_id"] == master.id
    assert marker["wipe_zigbee_pairing"] is False
    expires = datetime.fromisoformat(marker["expires_at"])
    assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(seconds=300)
    # No temp file left behind by the atomic write.
    assert not list(Path(hass.config.path("ga")).glob("*.tmp"))


@pytest.mark.asyncio
async def test_site_reset_status_reports_pending_then_the_addon_verdict(hass) -> None:
    _seed(hass)
    _write_device_pin(hass)
    master = await _make_master(hass)

    resp = await GASiteResetStatusView().get(_FakeRequest(hass, hass_user=master))
    assert _body(resp) == {"pending": False, "status": None}

    await GASiteResetRequestView().post(
        _FakeRequest(hass, {"confirm": "LÖSCHEN", "pin": DEVICE_PIN}, hass_user=master)
    )
    resp = await GASiteResetStatusView().get(_FakeRequest(hass, hass_user=master))
    assert _body(resp)["pending"] is True

    # ga_manager picks the marker up (deletes it) and reports back.
    Path(hass.config.path(RESET_REQUEST_FILE)).unlink()
    Path(hass.config.path(RESET_STATUS_FILE)).write_text(
        json.dumps({"state": "accepted", "job_id": "job-1"}), encoding="utf-8"
    )
    resp = await GASiteResetStatusView().get(_FakeRequest(hass, hass_user=master))
    assert _body(resp) == {
        "pending": False,
        "status": {"state": "accepted", "job_id": "job-1"},
    }


@pytest.mark.asyncio
async def test_site_reset_status_requires_master(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    stranger = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASiteResetStatusView().get(_FakeRequest(hass, hass_user=stranger))
    assert resp.status == 403
