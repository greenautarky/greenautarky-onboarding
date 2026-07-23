"""Tests for room-scoped dashboards (rooms.py).

The load-bearing rule is the scope decision: only a REAL sub-user may ever be
restricted. A device that was never put into household mode (no master flagged,
no sub-users — i.e. most of the fleet today) must keep seeing its whole house,
otherwise this feature would blank out working devices.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from greenautarky_onboarding import rooms
from greenautarky_onboarding.const import DOMAIN, MASTER_USERS_FILE
from greenautarky_onboarding.rooms import (
    SCOPE_ALL,
    SCOPE_ROOMS,
    STRATEGY_TYPE,
    GASubUserAssignRoomView,
    async_install_home_strategy,
    async_scope_for,
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


class _User:
    def __init__(self, uid: str, *, is_admin: bool = False, is_owner: bool = False) -> None:
        self.id = uid
        self.name = uid
        self.is_admin = is_admin
        self.is_owner = is_owner


def _seed(hass, state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = state if state is not None else {"completed": True}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


def _write_master_flag(hass, *user_ids: str) -> None:
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"masters": [{"ha_user_id": u} for u in user_ids]}))


async def _area(hass, name: str):
    from homeassistant.helpers import area_registry as ar

    return ar.async_get(hass).async_create(name)


# ─── scope decision ───────────────────────────────────────────────────────


async def test_unmanaged_device_sees_everything(hass) -> None:
    """No master, no sub-users = the fleet today. It must NOT go blank."""
    state = _seed(hass)
    await _area(hass, "Wohnzimmer")

    scope, reason, areas = await async_scope_for(hass, _User("u1"), state, set(), {})

    assert scope == SCOPE_ALL
    assert reason == "unmanaged"
    assert [a["name"] for a in areas] == ["Wohnzimmer"]


async def test_master_sees_every_room(hass) -> None:
    state = _seed(hass)
    await _area(hass, "Bad")
    await _area(hass, "Küche")

    scope, reason, areas = await async_scope_for(
        hass, _User("m1"), state, {"m1"}, {"s1": {"master": "m1"}}
    )

    assert (scope, reason) == (SCOPE_ALL, "master")
    assert len(areas) == 2


async def test_admin_sees_every_room(hass) -> None:
    state = _seed(hass)
    await _area(hass, "Bad")

    scope, reason, _ = await async_scope_for(
        hass, _User("a1", is_admin=True), state, {"m1"}, {"s1": {"master": "m1"}}
    )

    assert (scope, reason) == (SCOPE_ALL, "admin")


async def test_sub_user_sees_only_granted_rooms(hass) -> None:
    """The whole point of the feature."""
    wohnzimmer = await _area(hass, "Wohnzimmer")
    await _area(hass, "Schlafzimmer")
    state = _seed(hass, {"sub_user_areas": {"s1": [wohnzimmer.id]}})

    scope, reason, areas = await async_scope_for(
        hass, _User("s1"), state, {"m1"}, {"s1": {"master": "m1"}}
    )

    assert (scope, reason) == (SCOPE_ROOMS, "subuser")
    assert [a["name"] for a in areas] == ["Wohnzimmer"]


async def test_sub_user_without_grant_sees_nothing(hass) -> None:
    """Fail closed: an unassigned sub-user gets an empty list, never the house."""
    await _area(hass, "Wohnzimmer")
    state = _seed(hass)

    scope, reason, areas = await async_scope_for(
        hass, _User("s1"), state, {"m1"}, {"s1": {"master": "m1"}}
    )

    assert (scope, reason) == (SCOPE_ROOMS, "subuser")
    assert areas == []


async def test_legacy_tenant_without_parent_keeps_the_house(hass) -> None:
    """A tenant created before the master flag existed still owns his home."""
    await _area(hass, "Wohnzimmer")
    state = _seed(hass)

    scope, reason, areas = await async_scope_for(
        hass, _User("old"), state, {"m1"}, {"s1": {"master": "m1"}}
    )

    assert (scope, reason) == (SCOPE_ALL, "tenant")
    assert len(areas) == 1


# ─── assign_room: gating ──────────────────────────────────────────────────


async def test_assign_room_requires_master(hass) -> None:
    area = await _area(hass, "Bad")
    _seed(hass)
    _write_master_flag(hass, "m1")

    res = await GASubUserAssignRoomView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": "s1", "area_id": area.id, "assigned": True},
            hass_user=_User("not-a-master"),
        )
    )

    assert res.status == 403


async def test_assign_room_enforces_parent_relation(hass) -> None:
    """A master may only touch his OWN sub-users — never another master's."""
    area = await _area(hass, "Bad")
    _seed(hass, {"sub_users": {"s1": {"master": "OTHER-MASTER"}}})
    _write_master_flag(hass, "m1")

    res = await GASubUserAssignRoomView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": "s1", "area_id": area.id, "assigned": True},
            hass_user=_User("m1"),
        )
    )

    assert res.status == 403


async def test_assign_unknown_room_is_404(hass) -> None:
    _seed(hass, {"sub_users": {"s1": {"master": "m1"}}})
    _write_master_flag(hass, "m1")

    res = await GASubUserAssignRoomView().post(
        _FakeRequest(
            hass,
            {"sub_user_id": "s1", "area_id": "ghost", "assigned": True},
            hass_user=_User("m1"),
        )
    )

    assert res.status == 404


async def test_assign_and_revoke_round_trip(hass) -> None:
    area = await _area(hass, "Bad")
    state = _seed(hass, {"sub_users": {"s1": {"master": "m1"}}})
    _write_master_flag(hass, "m1")
    view = GASubUserAssignRoomView()

    await view.post(
        _FakeRequest(
            hass,
            {"sub_user_id": "s1", "area_id": area.id, "assigned": True},
            hass_user=_User("m1"),
        )
    )
    assert rooms.rooms_of(state, "s1") == [area.id]

    await view.post(
        _FakeRequest(
            hass,
            {"sub_user_id": "s1", "area_id": area.id, "assigned": False},
            hass_user=_User("m1"),
        )
    )
    assert rooms.rooms_of(state, "s1") == []


# ─── the default dashboard IS the strategy ────────────────────────────────


class _FakeDash:
    """Stands in for lovelace's LovelaceStorage of the default dashboard."""

    def __init__(self, config: dict[str, Any] | None) -> None:
        self.config = config
        self.saved: dict[str, Any] | None = None

    async def async_load(self, force: bool):
        if self.config is None:
            from homeassistant.components.lovelace.const import ConfigNotFound

            raise ConfigNotFound
        return self.config

    async def async_save(self, config: dict[str, Any]) -> None:
        self.saved = config
        self.config = config


def _install_lovelace(hass, default: _FakeDash) -> None:
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    class _Data:
        dashboards: ClassVar[dict] = {None: default}

    hass.data[LOVELACE_DATA] = _Data()


async def test_strategy_is_installed_on_a_virgin_default_dashboard(hass) -> None:
    dash = _FakeDash(None)  # no stored config = HA's auto-generated overview
    _install_lovelace(hass, dash)

    assert await async_install_home_strategy(hass) is True
    assert dash.saved == {"strategy": {"type": STRATEGY_TYPE}}


async def test_strategy_install_is_idempotent(hass) -> None:
    dash = _FakeDash({"strategy": {"type": STRATEGY_TYPE}})
    _install_lovelace(hass, dash)

    assert await async_install_home_strategy(hass) is False
    assert dash.saved is None  # nothing rewritten


async def test_hand_made_default_dashboard_is_never_clobbered(hass) -> None:
    """Someone took control of the Overview — their views must survive."""
    dash = _FakeDash({"views": [{"title": "Meins", "cards": []}]})
    _install_lovelace(hass, dash)

    assert await async_install_home_strategy(hass) is False
    assert dash.saved is None
    assert dash.config["views"][0]["title"] == "Meins"


# ─── server-side home model (#569) ─────────────────────────────────────────


async def test_home_model_excludes_stateless_and_config_entities(hass) -> None:
    """The root cause of the sub-user board crash: the registry lists entities
    absent from the user's scoped states (a device `update.*` config entity).
    The model must only carry entities that (a) have a live state and (b) are
    resident-facing — so the strategy never renders a card for a null entity."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.const import EntityCategory

    area = await _area(hass, "Wohnzimmer")
    reg = er.async_get(hass)

    # a resident-facing climate + a temperature sensor (both live)
    reg.async_get_or_create("climate", "test", "trv1", suggested_object_id="wohnzimmer_trv")
    reg.async_update_entity("climate.wohnzimmer_trv", area_id=area.id)
    hass.states.async_set("climate.wohnzimmer_trv", "heat", {})
    reg.async_get_or_create("sensor", "test", "temp1", suggested_object_id="wohnzimmer_temp")
    reg.async_update_entity("sensor.wohnzimmer_temp", area_id=area.id)
    hass.states.async_set("sensor.wohnzimmer_temp", "21.0", {"device_class": "temperature"})

    # a firmware-update config entity in the SAME room but WITHOUT a live state
    # (this is exactly what crashed the client: listed by the registry, null in
    # the scoped user's states).
    reg.async_get_or_create(
        "update", "test", "fw1", suggested_object_id="wohnzimmer_fw",
        entity_category=EntityCategory.CONFIG,
    )
    reg.async_update_entity("update.wohnzimmer_fw", area_id=area.id)
    # deliberately DO NOT set a state for update.wohnzimmer_fw

    areas = [{"area_id": area.id, "name": "Wohnzimmer"}]
    model = rooms._build_home_model(hass, _User("u1"), SCOPE_ALL, areas)

    assert len(model["rooms"]) == 1
    room = model["rooms"][0]
    assert room["climate"] == ["climate.wohnzimmer_trv"]
    assert room["temps"] == ["sensor.wohnzimmer_temp"]
    # the stateless config entity is NOWHERE in the model
    blob = json.dumps(model)
    assert "update.wohnzimmer_fw" not in blob


async def test_home_model_drops_empty_rooms(hass) -> None:
    """A room with no renderable entity is noise — omitted."""
    area = await _area(hass, "Leerraum")
    areas = [{"area_id": area.id, "name": "Leerraum"}]
    model = rooms._build_home_model(hass, _User("u1"), SCOPE_ALL, areas)
    assert model["rooms"] == []
