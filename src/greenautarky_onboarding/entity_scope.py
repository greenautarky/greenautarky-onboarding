"""Stage A of room scoping — turn a sub-user's assigned ROOMS into a NATIVE
per-user entity permission, so HA's own enforcement scopes what he can read and
control over the WS/REST API (``get_states``, ``subscribe_entities``,
``call_service``). This is the piece that upgrades room scoping from a
presentation filter (see :mod:`rooms`) to an actual access boundary on the
state + control planes.

How it works
------------
1. Compile the user's assigned areas into an explicit set of ``entity_ids``
   (effective area = ``entity.area_id`` OR the entity's ``device.area_id``).
   We compile to explicit entity_ids on purpose — HA's ``area_ids`` policy
   resolves the area via the *device only* and ignores ``entity.area_id``, so
   helpers / template entities / device-less entities would fall through.
2. Put the user in a per-user custom GROUP whose policy is exactly those
   entity_ids (``{"entities": {"entity_ids": {eid: {"read", "control"}}}}``) and
   nothing else, dropping ``system-users`` (which grants ALL entities).
3. HA's built-in ``user.permissions.check_entity`` does the rest.

Scope of the guarantee (read this before selling it)
----------------------------------------------------
This closes the state-read + service-control planes. It does NOT close
``history`` / ``logbook`` / registry-list / ``render_template`` — those HA paths
skip the permission check (Stage B, a separate command-wrapper, handles them).
Admins/owner are never scoped (HA bypasses them by design).

Gating
------
Default OFF. Enabling it CHANGES what a sub-user can see over the API, so it is
opt-in per device via the onboarding state flag ``entity_scoping_enabled`` (set by
the admin-only entity_scoping view). Masters, admins, owners and
unmanaged/legacy devices are never scoped (mirrors :func:`rooms.async_scope_for`).

⚠️ Uses ``hass.auth._store`` internals — there is no public API to create an auth
group or set a policy. Guard with a Core-version contract test before shipping.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import Group
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

STATE_ENABLED = "entity_scoping_enabled"  # bool in the onboarding state
GROUP_PREFIX = "ga_scope_"
_READ_CONTROL = {"read": True, "control": True}


def is_enabled(state: dict[str, Any]) -> bool:
    """Whether entity scoping is active on this device (default OFF)."""
    return bool(state.get(STATE_ENABLED))


def compile_entities(hass: HomeAssistant, area_ids: set[str]) -> set[str]:
    """Every entity_id whose EFFECTIVE area is in ``area_ids``.

    Effective area = the entity's own ``area_id``, else its device's — the entity
    override wins, and device-less entities are still matched by their own area.
    """
    if not area_ids:
        return set()
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    dev_area = {d.id: d.area_id for d in dev_reg.devices.values()}
    return {
        ent.entity_id
        for ent in ent_reg.entities.values()
        if (ent.area_id or dev_area.get(ent.device_id)) in area_ids
    }


def _group_id(user_id: str) -> str:
    return GROUP_PREFIX + user_id


def is_user_scoped(user: Any) -> bool:
    """True iff this user currently carries a Stage-A room scope.

    Ground truth = membership in a ``ga_scope_*`` group (added by ``async_apply``,
    removed by ``async_clear``). The leak-guard uses this so it tracks the
    APPLIED policy, not just the config flag: a user Core is not restricting is
    a user Stage B leaves alone.
    """
    return any(g.id.startswith(GROUP_PREFIX) for g in getattr(user, "groups", ()))


def _invalidate(user: Any) -> None:
    """Drop the cached permission object so the next request recomputes it."""
    if hasattr(user, "invalidate_cache"):
        user.invalidate_cache()
    else:  # older cores: clear the cached_property directly
        user.__dict__.pop("permissions", None)


async def async_apply(hass: HomeAssistant, user_id: str, area_ids: set[str]) -> int:
    """Scope ``user_id`` to exactly the entities of ``area_ids``. Returns the
    entity count. No-ops safely on owner (never lock the owner out)."""
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        _LOGGER.warning("entity_scope: no such user %s", user_id)
        return 0
    if user.is_owner:
        _LOGGER.warning("entity_scope: refusing to scope the owner")
        return 0

    entity_ids = compile_entities(hass, area_ids)
    policy = {"entities": {"entity_ids": {eid: dict(_READ_CONTROL) for eid in sorted(entity_ids)}}}

    gid = _group_id(user_id)
    store = hass.auth._store  # intentional: no public create-group API
    store._groups.pop(gid, None)
    store._groups[gid] = Group(
        name=f"GA scope: {user.name}", policy=policy, id=gid, system_generated=False
    )
    store._async_schedule_save()
    await hass.auth.async_update_user(user, group_ids=[gid])
    _invalidate(user)
    _LOGGER.info("entity_scope: %s scoped to %d entities (%d areas)", user.name, len(entity_ids), len(area_ids))
    return len(entity_ids)


async def async_clear(hass: HomeAssistant, user_id: str) -> None:
    """Remove the scope — put the user back in ``system-users`` (sees all)."""
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        return
    await hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
    hass.auth._store._groups.pop(_group_id(user_id), None)
    hass.auth._store._async_schedule_save()
    _invalidate(user)
    _LOGGER.info("entity_scope: %s unscoped (back to system-users)", user.name)


async def async_reconcile_user(hass: HomeAssistant, user_id: str, state: dict[str, Any]) -> None:
    """Apply the current room assignment for one sub-user (call after assign /
    on start). Only real sub-users are scoped; if scoping is disabled this
    clears any leftover scope so nothing is silently restricted."""
    from .rooms import STATE_ROOMS

    sub_users = state.get("sub_users") or {}
    if not is_enabled(state) or user_id not in sub_users:
        await async_clear(hass, user_id)
        return
    area_ids = set((state.get(STATE_ROOMS) or {}).get(user_id) or [])
    await async_apply(hass, user_id, area_ids)


async def async_reconcile_all(hass: HomeAssistant, state: dict[str, Any]) -> None:
    """Reconcile every sub-user (startup + toggle). Clears all if disabled."""
    from .rooms import STATE_ROOMS

    sub_users = state.get("sub_users") or {}
    matrix = state.get(STATE_ROOMS) or {}
    for user_id in set(sub_users) | set(matrix):
        await async_reconcile_user(hass, user_id, state)
