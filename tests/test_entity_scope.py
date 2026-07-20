"""Tests for Stage A entity scoping (entity_scope.py).

Two layers:
* unit — ``compile_entities`` (area -> entity_ids, incl. the entity/device area
  precedence that avoids HA's area_ids-policy trap);
* integration — ``async_apply``/``async_clear``/``async_reconcile_*`` against a
  REAL ``hass.auth`` store, asserting HA's own ``permissions.check_entity`` then
  scopes reads/controls. This is what makes the boundary real, not cosmetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from greenautarky_onboarding import entity_scope, rooms
from greenautarky_onboarding.const import DOMAIN, MASTER_USERS_FILE

READ, CONTROL = "read", "control"


# ─── helpers ───────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body=None, hass_user=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._items: dict[str, Any] = {"hass_user": hass_user}

    async def json(self) -> dict[str, Any]:
        return self._body

    def __getitem__(self, key):
        return self._items[key]


def _seed(hass, state: dict[str, Any]) -> dict[str, Any]:
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": state}
    return state


def _write_master_flag(hass, *user_ids: str) -> None:
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"masters": [{"ha_user_id": u} for u in user_ids]}))


async def _entity_in_area(hass, entity_id: str, area) -> None:
    """Register an entity and put it directly in an area."""
    domain, obj = entity_id.split(".", 1)
    reg = er.async_get(hass)
    ent = reg.async_get_or_create(domain, "test", entity_id, suggested_object_id=obj)
    reg.async_update_entity(ent.entity_id, area_id=area.id)


def _device_in_area(hass, obj: str, area):
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", obj)}
    )
    dr.async_get(hass).async_update_device(dev.id, area_id=area.id)
    return dev, entry


async def _entity_via_device(hass, entity_id: str, area) -> None:
    """Register an entity whose AREA comes from its device (entity.area_id unset)."""
    domain, obj = entity_id.split(".", 1)
    dev, entry = _device_in_area(hass, obj, area)
    er.async_get(hass).async_get_or_create(
        domain, "test", entity_id, suggested_object_id=obj,
        device_id=dev.id, config_entry=entry,
    )


async def _sub_user(hass, name="Sub"):
    """A NON-owner, non-admin user. HA makes the FIRST user the owner, so ensure
    an owner already exists (else this user would be owner and bypass scoping)."""
    if not any(u.is_owner for u in await hass.auth.async_get_users()):
        await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_USER])
    return await hass.auth.async_create_user(name, group_ids=[GROUP_ID_USER])


# ─── unit: compile_entities ────────────────────────────────────────────────


async def test_compile_by_entity_area(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    other = ar.async_get(hass).async_create("Other")
    await _entity_in_area(hass, "light.living", living)
    await _entity_in_area(hass, "light.other", other)

    assert entity_scope.compile_entities(hass, {living.id}) == {"light.living"}


async def test_compile_by_device_area(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_via_device(hass, "sensor.dev_temp", living)

    assert entity_scope.compile_entities(hass, {living.id}) == {"sensor.dev_temp"}


async def test_entity_area_overrides_device_area(hass) -> None:
    """entity.area_id wins over the device's area — the area_ids-policy trap."""
    living = ar.async_get(hass).async_create("Living")
    bath = ar.async_get(hass).async_create("Bath")
    # device is in Living, but the entity is explicitly moved to Bath
    dev, entry = _device_in_area(hass, "trv", living)
    ent = er.async_get(hass).async_get_or_create(
        "climate", "test", "climate.trv", suggested_object_id="trv",
        device_id=dev.id, config_entry=entry,
    )
    er.async_get(hass).async_update_entity(ent.entity_id, area_id=bath.id)

    assert entity_scope.compile_entities(hass, {bath.id}) == {"climate.trv"}
    assert entity_scope.compile_entities(hass, {living.id}) == set()


async def test_compile_empty_areas_is_empty(hass) -> None:
    assert entity_scope.compile_entities(hass, set()) == set()


# ─── integration: apply / clear against real auth ──────────────────────────


async def test_apply_scopes_permissions(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    await _entity_in_area(hass, "light.bath", ar.async_get(hass).async_create("Bath"))
    user = await _sub_user(hass)
    assert user.permissions.access_all_entities(READ) is True  # baseline: sees all

    n = await entity_scope.async_apply(hass, user.id, {living.id})

    assert n == 1
    assert [g.id for g in user.groups] == [entity_scope.GROUP_PREFIX + user.id]
    assert user.is_admin is False
    assert user.permissions.access_all_entities(READ) is False
    assert user.permissions.check_entity("light.living", READ) is True
    assert user.permissions.check_entity("light.living", CONTROL) is True
    assert user.permissions.check_entity("light.bath", READ) is False
    assert user.permissions.check_entity("light.bath", CONTROL) is False


async def test_clear_restores_all_access(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    user = await _sub_user(hass)
    await entity_scope.async_apply(hass, user.id, {living.id})
    assert user.permissions.access_all_entities(READ) is False

    await entity_scope.async_clear(hass, user.id)

    assert [g.id for g in user.groups] == [GROUP_ID_USER]
    assert user.permissions.access_all_entities(READ) is True
    assert entity_scope.GROUP_PREFIX + user.id not in hass.auth._store._groups


async def test_apply_refuses_owner(hass) -> None:
    owner = await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_USER])  # first = owner
    assert owner.is_owner is True
    assert await entity_scope.async_apply(hass, owner.id, set()) == 0
    assert owner.permissions.access_all_entities(READ) is True  # untouched


async def test_apply_empty_areas_scopes_to_nothing(hass) -> None:
    """A sub-user with no rooms is fail-closed: zero readable entities."""
    await _entity_in_area(hass, "light.x", ar.async_get(hass).async_create("Living"))
    user = await _sub_user(hass)
    await entity_scope.async_apply(hass, user.id, set())
    assert user.permissions.access_all_entities(READ) is False
    assert user.permissions.check_entity("light.x", READ) is False


# ─── gate + reconcile ───────────────────────────────────────────────────────


async def test_reconcile_disabled_clears(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    user = await _sub_user(hass)
    await entity_scope.async_apply(hass, user.id, {living.id})  # leftover scope
    state = {"sub_users": {user.id: {}}, "sub_user_areas": {user.id: [living.id]}}  # flag OFF

    await entity_scope.async_reconcile_user(hass, user.id, state)

    assert user.permissions.access_all_entities(READ) is True  # cleared


async def test_reconcile_subuser_applies_when_enabled(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    user = await _sub_user(hass)
    state = {
        entity_scope.STATE_ENABLED: True,
        "sub_users": {user.id: {}},
        "sub_user_areas": {user.id: [living.id]},
    }

    await entity_scope.async_reconcile_user(hass, user.id, state)

    assert user.permissions.access_all_entities(READ) is False
    assert user.permissions.check_entity("light.living", READ) is True


async def test_reconcile_non_subuser_is_never_scoped(hass) -> None:
    """A user that is not a sub-user (e.g. a master) is never restricted."""
    user = await _sub_user(hass, "Master")
    state = {entity_scope.STATE_ENABLED: True, "sub_users": {}, "sub_user_areas": {}}
    await entity_scope.async_reconcile_user(hass, user.id, state)
    assert user.permissions.access_all_entities(READ) is True


async def test_reconcile_all_covers_matrix_and_subusers(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    u = await _sub_user(hass)
    state = {
        entity_scope.STATE_ENABLED: True,
        "sub_users": {u.id: {}},
        "sub_user_areas": {u.id: [living.id]},
    }
    await entity_scope.async_reconcile_all(hass, state)
    assert u.permissions.access_all_entities(READ) is False


# ─── the assign_room hook applies the scope ─────────────────────────────────


async def test_assign_room_applies_scope_when_enabled(hass) -> None:
    living = ar.async_get(hass).async_create("Living")
    await _entity_in_area(hass, "light.living", living)
    master = await hass.auth.async_create_user("M", group_ids=[GROUP_ID_USER])
    object.__setattr__(master, "is_admin", True)
    sub = await _sub_user(hass)
    _write_master_flag(hass, master.id)
    state = _seed(
        hass,
        {
            entity_scope.STATE_ENABLED: True,
            "sub_users": {sub.id: {"master": master.id}},
        },
    )

    view = rooms.GASubUserAssignRoomView()
    req = _FakeRequest(
        hass, body={"sub_user_id": sub.id, "area_id": living.id, "assigned": True}, hass_user=master
    )
    resp = await view.post(req)

    assert resp.status == 200
    assert state["sub_user_areas"][sub.id] == [living.id]
    # the hook scoped the sub-user natively
    assert sub.permissions.access_all_entities(READ) is False
    assert sub.permissions.check_entity("light.living", READ) is True
