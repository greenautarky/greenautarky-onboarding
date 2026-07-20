"""Stage B — close the read paths Core does not check against the entity policy.

Stage A (``entity_scope``) installs a native per-user entity policy that Core
enforces on ``get_states`` / ``subscribe_entities`` / ``call_service`` / REST.
Core does NOT check it on history, logbook, the registry-list commands or
``render_template`` — a room-scoped sub-user can still learn about entities
outside their rooms through those. This module re-registers each of those
websocket commands with a wrapper that filters (or denies) the response for a
scoped user, and delegates untouched for everyone else.

Design: ``docs/STAGE-B-LEAK-WRAPPER.md``. Covers the request/response commands
(increment 1); the streaming variants (``history/stream`` /
``logbook/event_stream``) are increment 2.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.websocket_api import (
    async_register_command,
)
from homeassistant.components.websocket_api import (
    const as ws_const,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .entity_scope import is_user_scoped

_LOGGER = logging.getLogger(__name__)

_GUARD_MARKER = "_ga_leak_guarded"

# Commands we DENY outright for a scoped user — no safe way to output-filter.
DENY_COMMANDS = ("render_template",)

# Commands whose result we FILTER to permitted entities. The value names the
# per-command result shape so ``_filter_result`` knows how to walk it.
FILTER_COMMANDS: dict[str, str] = {
    "history/history_during_period": "entity_keyed_map",
    "logbook/get_events": "entity_row_list",
    "config/entity_registry/list": "entity_row_list",
    "config/entity_registry/list_for_display": "display_map",
    "config/entity_registry/get_entries": "entity_keyed_map",
    "config/device_registry/list": "device_row_list",
    "config/area_registry/list": "area_row_list",
}


# Streaming commands (increment 2): they push via ``send_message`` over the
# stream's lifetime, so output-interception is impractical — instead the
# REQUEST is pruned to permitted entities before delegation. Empty after
# pruning → deny. ``logbook/event_stream`` without any entity_ids/device_ids
# is a whole-home stream: for a scoped user the permitted entity list is
# INJECTED so the logbook panel keeps working, scoped to their rooms.
PRUNE_COMMANDS = ("history/stream", "logbook/event_stream")


def _may_read(user: Any, entity_id: str) -> bool:
    return bool(entity_id) and user.permissions.check_entity(entity_id, POLICY_READ)


def _permitted_entities(hass: HomeAssistant, user: Any) -> set[str]:
    ent_reg = er.async_get(hass)
    return {e.entity_id for e in ent_reg.entities.values() if _may_read(user, e.entity_id)}


def _filter_result(hass: HomeAssistant, user: Any, shape: str, result: Any) -> Any:
    """Return ``result`` with anything the user may not read removed."""
    if result is None:
        return result
    try:
        if shape == "entity_keyed_map" and isinstance(result, dict):
            # {entity_id: [...]} (history) / {entity_id: {...}} (get_entries)
            return {k: v for k, v in result.items() if _may_read(user, k)}
        if shape == "entity_row_list" and isinstance(result, list):
            # [{"entity_id": ...}, ...] (registry list / logbook rows)
            return [r for r in result
                    if not isinstance(r, dict) or _may_read(user, r.get("entity_id", ""))]
        if shape == "display_map" and isinstance(result, dict):
            # {"entities": [{"ei": <entity_id>, ...}], "categories": {...}}
            rows = result.get("entities")
            if isinstance(rows, list):
                result = dict(result)
                result["entities"] = [
                    r for r in rows
                    if not isinstance(r, dict) or _may_read(user, r.get("ei", ""))
                ]
            return result
        if shape == "device_row_list" and isinstance(result, list):
            permitted = _permitted_entities(hass, user)
            ent_reg = er.async_get(hass)
            keep = {e.device_id for e in ent_reg.entities.values()
                    if e.entity_id in permitted and e.device_id}
            return [r for r in result
                    if not isinstance(r, dict) or r.get("id") in keep]
        if shape == "area_row_list" and isinstance(result, list):
            permitted = _permitted_entities(hass, user)
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            dev_area = {d.id: d.area_id for d in dev_reg.devices.values()}
            keep = set()
            for e in ent_reg.entities.values():
                if e.entity_id in permitted:
                    keep.add(e.area_id or dev_area.get(e.device_id))
            return [r for r in result
                    if not isinstance(r, dict) or r.get("area_id") in keep]
    except Exception:  # hardening — a filter bug must not leak OR crash the socket
        _LOGGER.exception("leak_guard: filter for %s failed — denying to stay safe", shape)
        return [] if isinstance(result, list) else {}
    return result


def _pruned_stream_msg(
    hass: HomeAssistant, user: Any, command: str, msg: dict[str, Any]
) -> dict[str, Any] | None:
    """A copy of ``msg`` restricted to what the user may read — None = deny.

    ``history/stream`` requires entity_ids; ``logbook/event_stream`` may carry
    entity_ids and/or device_ids, or neither (whole home — inject the permitted
    list instead). Runs AFTER schema validation (the wrapper IS the handler).
    """
    out = dict(msg)
    requested = msg.get("entity_ids")
    if requested is not None:
        allowed = [e for e in requested if _may_read(user, e)]
        if not allowed:
            return None
        out["entity_ids"] = allowed

    device_ids = msg.get("device_ids")
    if device_ids is not None:
        permitted = _permitted_entities(hass, user)
        ent_reg = er.async_get(hass)
        keep = {e.device_id for e in ent_reg.entities.values()
                if e.entity_id in permitted and e.device_id}
        allowed_devs = [d for d in device_ids if d in keep]
        if not allowed_devs and requested is None:
            return None
        if allowed_devs:
            out["device_ids"] = allowed_devs
        else:
            out.pop("device_ids", None)

    if command == "logbook/event_stream" and requested is None and device_ids is None:
        permitted = sorted(_permitted_entities(hass, user))
        if not permitted:
            return None
        out["entity_ids"] = permitted
    return out


def _wrap(hass: HomeAssistant, command: str, original: Callable, shape: str | None) -> Callable:
    @callback
    def guarded(hass_: HomeAssistant, connection: Any, msg: dict[str, Any]) -> Any:
        user = connection.user
        if getattr(user, "is_admin", False) or getattr(user, "is_owner", False) \
                or not is_user_scoped(user):
            return original(hass_, connection, msg)

        if command in DENY_COMMANDS:
            connection.send_error(
                msg["id"], ws_const.ERR_UNAUTHORIZED,
                "not permitted for a room-scoped user",
            )
            return None

        if command in PRUNE_COMMANDS:
            pruned = _pruned_stream_msg(hass_, user, command, msg)
            if pruned is None:
                connection.send_error(
                    msg["id"], ws_const.ERR_UNAUTHORIZED,
                    "no permitted entities in this request",
                )
                return None
            return original(hass_, connection, pruned)

        # Intercept the single result the handler sends, filter, restore.
        orig_send_result = connection.send_result

        @callback
        def filtered_send_result(msg_id: Any, result: Any = None) -> None:
            orig_send_result(msg_id, _filter_result(hass_, user, shape or "", result))

        connection.send_result = filtered_send_result
        try:
            res = original(hass_, connection, msg)
            if asyncio.iscoroutine(res):
                async def _await_then_restore() -> None:
                    try:
                        await res
                    finally:
                        connection.send_result = orig_send_result
                return _await_then_restore()
            connection.send_result = orig_send_result
            return res
        except Exception:
            connection.send_result = orig_send_result
            raise

    guarded._ga_leak_guarded = True  # type: ignore[attr-defined]
    # The schema travels separately in the registry tuple, so install() must
    # re-supply the original schema when it re-registers this wrapper.
    return guarded


@callback
def install(hass: HomeAssistant) -> int:
    """Re-register the leaky WS commands with scoping wrappers. Idempotent.

    Returns the number of commands guarded (for logging/tests). Safe to call
    unconditionally: with no scoped users every wrapper is a pass-through.
    """
    registry = hass.data.get(ws_const.DOMAIN)
    if not registry:
        return 0
    guarded = 0
    for command in (*DENY_COMMANDS, *PRUNE_COMMANDS, *FILTER_COMMANDS):
        entry = registry.get(command)
        if entry is None:
            continue  # component not loaded on this device — nothing to guard
        handler, schema = entry
        if getattr(handler, _GUARD_MARKER, False):
            guarded += 1
            continue  # already wrapped (idempotent re-install)
        shape = FILTER_COMMANDS.get(command)
        wrapped = _wrap(hass, command, handler, shape)
        async_register_command(hass, command, wrapped, schema)
        guarded += 1
    _LOGGER.info("leak_guard: guarding %d websocket command(s)", guarded)
    return guarded
