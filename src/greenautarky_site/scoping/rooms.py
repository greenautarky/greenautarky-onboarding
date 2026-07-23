"""Room-scoped dashboards — the master assigns ROOMS, the dashboard is generated.

Replaces the old ``[sub-user x dashboard]`` matrix (and the per-user dashboards it
needed) with a ``[sub-user x room]`` matrix. There is exactly ONE dashboard on the
device — HA's default Overview — and its stored config is nothing but a strategy
stub::

    {"strategy": {"type": "custom:ga-home"}}

The strategy (shipped by ga-frontend-bundle) runs in the browser on every load,
asks :class:`GAMyRoomsView` who the logged-in user is and what he may see, and
builds the views from HA's own area/device/entity registries.

Consequences, all of them deliberate:

* **No per-user dashboard is stored.** No panel registration, no boot
  re-registration, no per-view ``visible`` reconcile — and therefore none of the
  orphan-board failure modes that class of code had (KB #149 §5a).
* **Nothing is user-editable.** Saving a Lovelace config is admin-only in HA, so a
  Non-Admin never could edit his board anyway; now there is nothing to edit.

Scope is decided HERE, on the server, and returned WITH its reason so the frontend
never has to guess:

===========================  ========  ==============================================
situation                    scope     why
===========================  ========  ==============================================
no master AND no sub-users    all      device was never put into household mode
caller is a master            all      he runs the household
caller is admin / owner       all      operator / support
caller IS a sub-user          rooms    the feature (empty list -> honest empty state)
plain tenant, no parent       all      legacy device; it is his house
===========================  ========  ==============================================

Only a real sub-user is ever restricted. **A device we have not configured can
never be made poorer by this feature** — that is the load-bearing rule, because
most of the fleet today has neither a master flag nor a single HA area.

⚠️ By itself this is PRESENTATION scoping, not isolation: HA serves every entity
to any authenticated non-admin over the WebSocket API. To make it an actual
boundary on the state-read + service-control planes, enable :mod:`entity_scope`
(Stage A, default OFF) — it compiles the room assignment into a native per-user
entity permission. Even then ``render_template`` / history / logbook / registry
lists stay open until the Stage B wrapper closes them. Do not sell room scoping
as tenant isolation unless BOTH stages are on.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..household.masters import _read_master_user_ids, _require_master
from ..household.sub_users import _children_of
from ..store import _get_state, _get_store

_LOGGER = logging.getLogger(__name__)

STRATEGY_TYPE = "custom:ga-home"
STRATEGY_CONFIG: dict[str, Any] = {"strategy": {"type": STRATEGY_TYPE}}

SCOPE_ALL = "all"
SCOPE_ROOMS = "rooms"

STATE_ROOMS = "sub_user_areas"  # {user_id: [area_id, ...]}


def room_matrix(state: dict[str, Any]) -> dict[str, list[str]]:
    """The ``{user_id: [area_id]}`` matrix (created on first use)."""
    return state.setdefault(STATE_ROOMS, {})


def rooms_of(state: dict[str, Any], user_id: str) -> list[str]:
    return list((state.get(STATE_ROOMS) or {}).get(user_id) or [])


def _areas(hass: HomeAssistant) -> list[dict[str, str]]:
    return [
        {"area_id": a.id, "name": a.name}
        for a in sorted(ar.async_get(hass).async_list_areas(), key=lambda a: a.name or "")
    ]


async def async_scope_for(
    hass: HomeAssistant,
    user: Any,
    state: dict[str, Any],
    masters: set[str],
    sub_users: dict[str, Any],
) -> tuple[str, str, list[dict[str, str]]]:
    """``(scope, reason, areas)`` for one user — the single place this is decided."""
    uid = getattr(user, "id", None)

    # An unconfigured device must never render an empty house.
    if not masters and not sub_users:
        return SCOPE_ALL, "unmanaged", _areas(hass)

    if uid in masters:
        return SCOPE_ALL, "master", _areas(hass)

    if getattr(user, "is_admin", False) or getattr(user, "is_owner", False):
        return SCOPE_ALL, "admin", _areas(hass)

    if uid in sub_users:
        allowed = set(rooms_of(state, uid))
        return SCOPE_ROOMS, "subuser", [a for a in _areas(hass) if a["area_id"] in allowed]

    # A tenant that predates the master flag — it is his house.
    return SCOPE_ALL, "tenant", _areas(hass)


# ---------------------------------------------------------------------------
# The default dashboard IS the strategy
# ---------------------------------------------------------------------------


async def async_install_home_strategy(hass: HomeAssistant) -> bool:
    """Make HA's default Overview render our per-user strategy.

    HA's default dashboard (``url_path`` = ``None``, storage key ``lovelace``) is the
    only panel that cannot be removed or hidden — so instead of fighting it, we own
    its config. With no stored config HA falls back to its auto-generated
    ``original-states`` overview; we replace exactly that.

    Never clobbers a config we did not write: if someone took control of the default
    dashboard and put real views in it, we leave it alone and say so. Idempotent.
    """
    try:
        from homeassistant.components.lovelace import dashboard as lovelace_dashboard
        from homeassistant.components.lovelace.const import (
            LOVELACE_DATA,
            ConfigNotFound,
        )
    except ImportError:  # pragma: no cover - lovelace is always there on GA OS
        _LOGGER.debug("home-strategy: lovelace not available")
        return False

    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        _LOGGER.debug("home-strategy: lovelace not set up yet")
        return False

    default = data.dashboards.get(None)
    if default is None:  # pragma: no cover - lovelace always registers it
        default = lovelace_dashboard.LovelaceStorage(hass, None)
        data.dashboards[None] = default

    try:
        current = await default.async_load(False)
    except ConfigNotFound:
        current = None
    except Exception as err:
        _LOGGER.warning("home-strategy: cannot read the default dashboard: %s", err)
        return False

    if current:
        if (current.get("strategy") or {}).get("type") == STRATEGY_TYPE:
            return False  # already ours
        if current.get("views"):
            _LOGGER.info(
                "home-strategy: default dashboard has hand-made views — leaving it alone"
            )
            return False

    try:
        await default.async_save(dict(STRATEGY_CONFIG))
    except Exception as err:
        _LOGGER.warning("home-strategy: cannot write the default dashboard: %s", err)
        return False

    _LOGGER.info("home-strategy: default dashboard now renders %s", STRATEGY_TYPE)
    return True


# ---------------------------------------------------------------------------
# HTTP views
# ---------------------------------------------------------------------------


class GAMyRoomsView(HomeAssistantView):
    """What the CALLING user may see, and why. The strategy renders from this."""

    url = "/api/greenautarky_site/my_rooms"
    name = "api:greenautarky_site:my_rooms"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        state = _get_state(hass)
        masters = await hass.async_add_executor_job(_read_master_user_ids, hass)
        sub_users = state.get("sub_users") or {}

        scope, reason, areas = await async_scope_for(hass, user, state, masters, sub_users)
        return self.json(
            {
                "scope": scope,
                "reason": reason,
                "areas": areas,
                "areas_exist": bool(_areas(hass)),
                "is_master": reason == "master",
            }
        )


def _build_home_model(
    hass: HomeAssistant, user: Any, scope: str, areas: list[dict[str, str]]
) -> dict[str, Any]:
    """Compute the READY, already-scoped, states-validated home model.

    The ga-home strategy used to re-derive all of this in the browser from the
    device/entity registries and re-apply scope client-side. For a room-scoped
    sub-user that broke: the leak-guard-filtered registry lists entities absent
    from that user's scoped ``hass.states`` (e.g. a device ``update.*`` config
    entity) and a tile then read ``hass.states[id]`` = null → the whole board
    crashed (K0, 2026-07-22). We compute it HERE with the server's full ``hass``
    but return ONLY entities the calling user can actually see, so nothing null
    ever reaches the client and the strategy is pure presentation.

    An entity is included iff it (a) is not hidden/disabled, (b) is a LIVE state,
    and (c) for a scoped sub-user, passes the SAME entity read-permission HA uses
    (``user.permissions.check_entity``) — so the model matches the user's own
    state machine exactly. Admin/master/tenant (scope != rooms) see everything.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    allowed = {a["area_id"] for a in areas}
    area_name = {a["area_id"]: a["name"] for a in areas}
    perms = getattr(user, "permissions", None)
    scoped = scope == SCOPE_ROOMS

    def can_see(entity_id: str) -> bool:
        if hass.states.get(entity_id) is None:  # not a live state → never render
            return False
        if scoped and perms is not None:
            return bool(perms.check_entity(entity_id, "read"))
        return True

    def area_of(entry: Any) -> str | None:
        if entry.area_id:
            return entry.area_id
        dev = dev_reg.async_get(entry.device_id) if entry.device_id else None
        return dev.area_id if dev else None

    per_area: dict[str, list[Any]] = {}
    roomless: list[Any] = []
    for entry in ent_reg.entities.values():
        if entry.hidden_by or entry.disabled_by or not can_see(entry.entity_id):
            continue
        aid = area_of(entry)
        if aid and aid in allowed:
            per_area.setdefault(aid, []).append(entry)
        elif not aid and not scoped:  # roomless entities only for a house-wide user
            roomless.append(entry)

    def device_class(entity_id: str) -> str | None:
        st = hass.states.get(entity_id)
        return st.attributes.get("device_class") if st else None

    def classify(entries: list[Any]) -> dict[str, list[str]]:
        # entity_category is None == resident-facing control; config/diagnostic are
        # knobs/telemetry the resident does not operate (matches the old strategy).
        primary = [e for e in entries if e.entity_category is None]

        def dom(items: list[Any], d: str) -> list[str]:
            return [e.entity_id for e in items if e.entity_id.startswith(d + ".")]

        def sensors(items: list[Any], dc: str) -> list[str]:
            return [e.entity_id for e in items if device_class(e.entity_id) == dc]

        return {
            "climate": dom(primary, "climate"),  # strategy collapses coupled TRVs
            "lights": dom(primary, "light"),
            "switches": dom(primary, "switch"),
            "temps": sensors(primary, "temperature"),
            "hums": sensors(primary, "humidity"),
            "batts": sensors(entries, "battery"),  # battery is diagnostic → all entries
        }

    rooms: list[dict[str, Any]] = []
    for aid in sorted(per_area, key=lambda a: area_name.get(a, "")):
        cats = classify(per_area[aid])
        if not any(cats.values()):
            continue  # an empty room is noise
        rooms.append({"area_id": aid, "name": area_name.get(aid, aid), **cats})

    out: dict[str, Any] = {"rooms": rooms}
    if roomless:
        out["roomless"] = classify(roomless)
    return out


class GAHomeModelView(HomeAssistantView):
    """The READY scoped home model — the ga-home strategy renders straight from it.

    Server-computed so the strategy never touches the device/entity registries or
    re-derives scope client-side (which broke for sub-users on null states).
    """

    url = "/api/greenautarky_site/home_model"
    name = "api:greenautarky_site:home_model"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        state = _get_state(hass)
        masters = await hass.async_add_executor_job(_read_master_user_ids, hass)
        sub_users = state.get("sub_users") or {}

        scope, reason, areas = await async_scope_for(hass, user, state, masters, sub_users)
        model = _build_home_model(hass, user, scope, areas)
        return self.json(
            {
                "scope": scope,
                "reason": reason,
                "is_master": reason == "master",
                "user_name": getattr(user, "name", "") or "",
                "areas_exist": bool(_areas(hass)),
                **model,
            }
        )


class GASubUserAssignRoomView(HomeAssistantView):
    """Master-only: grant/revoke ONE room for ONE of the master's OWN sub-users."""

    url = "/api/greenautarky_site/sub_user/assign_room"
    name = "api:greenautarky_site:sub_user_assign_room"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        sub_user_id = (body.get("sub_user_id") or "").strip()
        area_id = (body.get("area_id") or "").strip()
        assigned = bool(body.get("assigned", True))
        if not sub_user_id or not area_id:
            return self.json_message("sub_user_id and area_id are required", status_code=400)

        state = _get_state(hass)
        if sub_user_id not in _children_of(state, master.id):
            return web.json_response({"message": "Not your sub-user"}, status=403)
        if area_id not in {a["area_id"] for a in _areas(hass)}:
            return web.json_response({"message": "Unknown area"}, status=404)

        matrix = room_matrix(state)
        rooms = set(matrix.get(sub_user_id) or [])
        if assigned:
            rooms.add(area_id)
        else:
            rooms.discard(area_id)
        matrix[sub_user_id] = sorted(rooms)
        await _get_store(hass).async_save(state)

        # Stage A: if entity scoping is enabled on this device, turn the new room
        # set into a native per-user entity permission. No-op (and self-healing)
        # when disabled — see entity_scope.async_reconcile_user.
        from . import entity_scope

        await entity_scope.async_reconcile_user(hass, sub_user_id, state)

        _LOGGER.info(
            "rooms: master %s %s %s for sub-user %s",
            master.id,
            "granted" if assigned else "revoked",
            area_id,
            sub_user_id,
        )
        return self.json({"status": "ok", "areas": matrix[sub_user_id]})


class GAEntityScopingView(HomeAssistantView):
    """Admin-only: turn the Stage-A entity boundary on/off for this device.

    GET  -> {"enabled": bool}
    POST {"enabled": bool} -> set the flag + reconcile every sub-user's scope.
    Default is OFF: room scoping stays presentation-only until an operator
    explicitly enables the enforcement (it changes what sub-users see over the
    API, so it must be a deliberate per-device choice).
    """

    url = "/api/greenautarky_site/entity_scoping"
    name = "api:greenautarky_site:entity_scoping"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        from . import entity_scope

        hass: HomeAssistant = request.app["hass"]
        return self.json({"enabled": entity_scope.is_enabled(_get_state(hass))})

    async def post(self, request: web.Request) -> web.Response:
        from . import entity_scope

        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        if not (getattr(user, "is_admin", False) or getattr(user, "is_owner", False)):
            return web.json_response({"message": "admin only"}, status=403)

        body = await request.json()
        enabled = bool(body.get("enabled", False))
        state = _get_state(hass)
        state[entity_scope.STATE_ENABLED] = enabled
        await _get_store(hass).async_save(state)
        await entity_scope.async_reconcile_all(hass, state)
        _LOGGER.warning("entity_scope: enforcement %s by %s", "ENABLED" if enabled else "disabled", user.id)
        return self.json({"enabled": enabled})
