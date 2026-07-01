"""Tests for the Master management plane (PROTOTYPE) — ADR-0006.

Covers set_master (admin-only), the master-gated list, dashboard assignment
(matrix + real per-view ``visible`` reconcile against a LovelaceStorage), and
room (area) rename. Entity rename is intentionally deferred (no tests).
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER

from greenautarky_onboarding.const import DOMAIN, MASTER_USERS_FILE
from greenautarky_onboarding.http import (
    GASubUserAssignDashboardView,
    GASubUserInviteView,
    GASubUserJoinView,
    GASubUserManageView,
    GASubUserRenameAreaView,
    GASubUserSetMasterView,
    _read_master_user_ids,
)


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
        _FakeRequest(hass, {"name": name, "password": "secret-pw-123", "invite_pin": pin})
    )
    assert resp.status == 200, _body(resp)
    return next(u for u in await hass.auth.async_get_users() if u.name == name)


# --------------------------------------------------------------------------- #
# set_master (admin-only)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_master_requires_admin(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    # Create an owner/admin first so the next user is a genuine non-admin
    # (HA makes the very first user the owner).
    await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    non_admin = await hass.auth.async_create_user("U", group_ids=[GROUP_ID_USER])
    assert not non_admin.is_admin
    resp = await GASubUserSetMasterView().post(
        _FakeRequest(hass, {"user_id": "x"}, hass_user=non_admin)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_set_master_flags_and_unflags(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    admin = await hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
    target = await hass.auth.async_create_user("Target", group_ids=[GROUP_ID_USER])

    r = await GASubUserSetMasterView().post(
        _FakeRequest(hass, {"user_id": target.id, "master": True}, hass_user=admin)
    )
    assert r.status == 200
    assert target.id in _read_master_user_ids(hass)

    r2 = await GASubUserSetMasterView().post(
        _FakeRequest(hass, {"user_id": target.id, "master": False}, hass_user=admin)
    )
    assert r2.status == 200
    assert target.id not in _read_master_user_ids(hass)


# --------------------------------------------------------------------------- #
# list — master-gated
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_requires_master(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    nobody = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASubUserManageView().get(_FakeRequest(hass, {}, hass_user=nobody))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_list_returns_only_own_children(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    kid = await _join_sub_user(hass, master, name="Kid")
    # a sub-user of a different master must not appear
    hass.data[DOMAIN]["state"].setdefault("sub_users", {})["other"] = {"master": "someone-else"}

    resp = await GASubUserManageView().get(_FakeRequest(hass, {}, hass_user=master))
    assert resp.status == 200
    data = _body(resp)
    ids = [s["user_id"] for s in data["sub_users"]]
    assert kid.id in ids
    assert "other" not in ids
    assert "areas" in data and "dashboards" in data
    # each sub-user carries its login-enabled state (for the card's toggle)
    kid_row = next(s for s in data["sub_users"] if s["user_id"] == kid.id)
    assert kid_row["active"] is True


# --------------------------------------------------------------------------- #
# assign_dashboard — matrix + real reconcile of per-view ``visible``
# --------------------------------------------------------------------------- #


def _inject_storage_dashboard(hass):
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    dash = LovelaceStorage(
        hass, {"id": "abc123", "url_path": "family", "title": "Family"}
    )
    hass.data[LOVELACE_DATA] = types.SimpleNamespace(dashboards={"family": dash})
    return dash


@pytest.mark.asyncio
async def test_assign_dashboard_reconciles_visibility(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    kid = await _join_sub_user(hass, master, name="Kid")

    dash = _inject_storage_dashboard(hass)
    await dash.async_save({"views": [{"title": "V1"}, {"title": "V2"}]})

    resp = await GASubUserAssignDashboardView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": kid.id, "url_path": "family", "assigned": True},
            hass_user=master,
        )
    )
    assert resp.status == 200, _body(resp)
    assert "family" in hass.data[DOMAIN]["state"]["sub_user_dashboards"][kid.id]

    cfg = await dash.async_load(False)
    for view in cfg["views"]:
        users = {v["user"] for v in view["visible"]}
        assert kid.id in users  # assigned sub-user sees it
        assert master.id in users  # master keeps visibility

    # Unassign → ``visible`` stripped (visible to all again).
    await GASubUserAssignDashboardView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": kid.id, "url_path": "family", "assigned": False},
            hass_user=master,
        )
    )
    cfg2 = await dash.async_load(False)
    assert all("visible" not in v for v in cfg2["views"])


@pytest.mark.asyncio
async def test_assign_dashboard_rejects_foreign_sub_user(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    _inject_storage_dashboard(hass)
    # 'stranger' is not this master's child
    hass.data[DOMAIN]["state"].setdefault("sub_users", {})["stranger"] = {"master": "x"}
    resp = await GASubUserAssignDashboardView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": "stranger", "url_path": "family", "assigned": True},
            hass_user=master,
        )
    )
    assert resp.status == 403


# --------------------------------------------------------------------------- #
# rename_area
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_area(hass) -> None:
    from homeassistant.helpers import area_registry as ar

    _seed(hass)
    master = await _make_master(hass)
    reg = ar.async_get(hass)
    area = reg.async_create("Wohnzimmer")

    resp = await GASubUserRenameAreaView().post(
        _FakeRequest(
            hass, {"area_id": area.id, "name": "Wohnzimmer EG"}, hass_user=master
        )
    )
    assert resp.status == 200, _body(resp)
    assert reg.async_get_area(area.id).name == "Wohnzimmer EG"


@pytest.mark.asyncio
async def test_rename_area_requires_master(hass) -> None:
    _seed(hass)
    await _ensure_auth_provider(hass)
    nobody = await hass.auth.async_create_user("Nobody", group_ids=[GROUP_ID_USER])
    resp = await GASubUserRenameAreaView().post(
        _FakeRequest(hass, {"area_id": "x", "name": "Y"}, hass_user=nobody)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_rename_area_unknown(hass) -> None:
    _seed(hass)
    master = await _make_master(hass)
    resp = await GASubUserRenameAreaView().post(
        _FakeRequest(hass, {"area_id": "nope", "name": "Y"}, hass_user=master)
    )
    assert resp.status == 404
