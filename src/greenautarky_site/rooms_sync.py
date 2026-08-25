"""Take a room list from GACI and make it real in Home Assistant.

The installer enters the flat's rooms in the GACI app during installation, before
anyone has onboarded. Until now those rooms lived only in the cloud database, and
a separate GitHub workflow SSHed into the device as root to materialise them.
This is that step, done from inside Core instead.

WHY IN THE COMPONENT AND NOT IN ga_manager
------------------------------------------
ga_manager owns the job plumbing and would be the obvious home, but it cannot do
the write. It holds no long-lived owner token — ``set_ha_location`` writes
``configuration.yaml`` and restarts Core precisely because of that — and the area
registry has no YAML equivalent; it is WebSocket-only. Hand-writing
``.storage/core.area_registry`` under a running Core is not an option either:
Core holds the registry in memory and flushes it lazily, so a hand-written file
is silently overwritten.

Only in-process code has the registries without a token. That is here.

The call arrives from ga_manager through Supervisor's ``/core/api/…`` proxy,
which authenticates as Supervisor — so this is a normal ``requires_auth = True``
view and no unauthenticated surface is added.

WHAT DOES NOT HAPPEN HERE
-------------------------
No pseudonym. ``area_ref`` is ``sha256(salt + area_id)`` and the salt lives in
ga_manager's add-on volume, which this container cannot read. That is deliberate,
not an obstacle: this view returns the plain ``area_id``, ga_manager converts it
to ``area_ref`` with its own salt, and the plain value never leaves the device.

No catalogue either. ``type`` is taken as the desired ``area_id`` and validated
only for shape. The pinned room-type catalogue lives in ga_manager
(``ha_areas.CATALOGUE``) and stays a single source of truth rather than being
copied here to drift.

MERGE IS THE ONLY MODE
----------------------
GACI owns the rooms at installation; the resident owns them afterwards. A
``replace`` mode would be a way to delete a resident's rooms from the cloud, so
it does not exist. A request asking for one is refused rather than silently
downgraded.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from .store import _get_state, _get_store

_LOGGER = logging.getLogger(__name__)

# A room type is used verbatim as the target area_id, so it must look like one.
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Zigbee IEEE address as it appears in a device registry identifier.
_IEEE_RE = re.compile(r"0x[0-9a-fA-F]{16}")

MAX_ROOMS = 64
MAX_MEMBERS_PER_ROOM = 128

# state key: {area_id: name GACI installed}. Lets a later sync report that the
# resident has renamed a room WITHOUT the new name ever leaving the device.
STATE_KEY = "rooms_sync_installed_names"


def _norm(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def _catalogue_name(room_type: str) -> str:
    """The English name whose slug IS the type — "dining_room" -> "Dining Room".

    Areas are created under this name so Home Assistant derives the area_id we
    want, and renamed to the installer's name immediately after. area_id is fixed
    at creation and survives renames, so both properties hold at once: the id
    stays catalogue-shaped (which is what makes the fleet-wide room type work)
    while the resident sees a name in their own language.
    """
    return room_type.replace("_", " ").title()


def _ieee_index(hass: HomeAssistant) -> dict[str, str]:
    """{ieee (lowercase): HA device_id} for every Zigbee device in the registry."""
    out: dict[str, str] = {}
    for device in dr.async_get(hass).devices.values():
        for pair in device.identifiers:
            # Identifiers are (domain, value) tuples. Scan the whole pair rather
            # than assuming a domain, so a future integration rename does not
            # silently unmap every device.
            match = _IEEE_RE.search(" ".join(str(p) for p in pair))
            if match:
                out[match.group(0).lower()] = device.id
                break
    return out


def _find_area(registry: ar.AreaRegistry, room_type: str | None, name: str):
    """Match an existing area: by id (== type) first, then by name.

    By id first because on a fresh device the seeded defaults land as catalogue
    ids (measured on a reflashed K31: living_room / kitchen / bedroom), so this
    is the common case, not the edge case — it renames those three into the
    installer's names instead of creating duplicates beside them.
    """
    if room_type:
        existing = registry.async_get_area(room_type)
        if existing is not None:
            return existing, "id"
    wanted = _norm(name)
    for area in registry.async_list_areas():
        if _norm(area.name) == wanted:
            return area, "name"
    return None, None


class GARoomsSyncView(HomeAssistantView):
    """``POST /api/greenautarky_site/rooms/sync`` — create rooms, place devices.

    Body::

        {"rooms": [{"name": "Wohnzimmer",
                    "type": "living_room",          # optional but wanted
                    "members": ["0x00158d000abcd001"]}]}

    Idempotent: running it twice changes nothing the second time. That matters
    because it runs again on every follow-up visit, and because a reflashed
    device needs a re-sync — it has lost its areas, so this is the recovery path
    as well as the install path.
    """

    url = "/api/greenautarky_site/rooms/sync"
    name = "api:greenautarky_site:rooms_sync"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]

        try:
            body = await request.json()
        except ValueError:
            return self.json({"message": "body is not JSON"}, status_code=400)

        mode = body.get("mode", "merge")
        if mode != "merge":
            # Refused, not downgraded: a caller asking for `replace` believes it
            # can delete rooms, and quietly doing something else would leave it
            # believing that.
            return self.json(
                {"message": f"mode {mode!r} is not supported; merge is the only "
                            "mode — GACI owns rooms at installation, the resident "
                            "owns them afterwards"},
                status_code=400,
            )

        rooms = body.get("rooms")
        if not isinstance(rooms, list) or not rooms:
            return self.json({"message": "rooms must be a non-empty list"}, status_code=400)
        if len(rooms) > MAX_ROOMS:
            return self.json(
                {"message": f"{len(rooms)} rooms exceeds the limit of {MAX_ROOMS}"},
                status_code=400,
            )

        area_reg = ar.async_get(hass)
        dev_reg = dr.async_get(hass)
        by_ieee = _ieee_index(hass)

        state = _get_state(hass)
        installed: dict[str, str] = dict(state.get(STATE_KEY) or {})

        results: list[dict[str, Any]] = []
        for entry in rooms:
            if not isinstance(entry, dict):
                return self.json({"message": "each room must be an object"}, status_code=400)

            name = str(entry.get("name") or "").strip()
            if not name:
                return self.json({"message": "every room needs a name"}, status_code=400)

            room_type = entry.get("type")
            if room_type is not None:
                room_type = str(room_type).strip().lower()
                if not _TYPE_RE.match(room_type):
                    return self.json(
                        {"message": f"room type {room_type!r} is not a valid slug"},
                        status_code=400,
                    )

            members = entry.get("members") or []
            if not isinstance(members, list):
                return self.json({"message": "members must be a list"}, status_code=400)
            if len(members) > MAX_MEMBERS_PER_ROOM:
                return self.json(
                    {"message": f"room {name!r} has more than "
                                f"{MAX_MEMBERS_PER_ROOM} members"},
                    status_code=400,
                )

            area, matched_on = _find_area(area_reg, room_type, name)
            created = area is None

            if created:
                # Created under the catalogue name so the id comes out as the
                # type, then renamed. See _catalogue_name.
                seed_name = _catalogue_name(room_type) if room_type else name
                area = area_reg.async_create(seed_name)
                if room_type and area.id != room_type:
                    # Not fatal — the room exists and works. But area_kind will
                    # fall back to "custom" for it, so the fleet-wide room type
                    # is lost for this room and that should be visible.
                    _LOGGER.warning(
                        "rooms-sync: wanted area_id %r for %r but Home Assistant "
                        "assigned %r — its room type will read as 'custom'",
                        room_type, name, area.id,
                    )
                if area.name != name:
                    area = area_reg.async_update(area.id, name=name)

            renamed_by_resident = False
            if not created:
                previous = installed.get(area.id)
                if previous is not None and _norm(previous) != _norm(area.name):
                    # The resident renamed it. Report THAT, never the new name —
                    # one bit, no content.
                    renamed_by_resident = True
                elif _norm(area.name) != _norm(name):
                    # Never installed under this name and not renamed by anyone:
                    # this is the seeded default being adopted. Take the
                    # installer's name.
                    area = area_reg.async_update(area.id, name=name)

            installed[area.id] = name

            assigned, unknown = 0, []
            for raw in members:
                ieee = str(raw).strip().lower()
                device_id = by_ieee.get(ieee)
                if device_id is None:
                    # Almost always "not interviewed yet" rather than an error:
                    # the sensor has not appeared in the device registry. Named
                    # so the caller can retry rather than assuming success.
                    unknown.append(ieee)
                    continue
                dev_reg.async_update_device(device_id, area_id=area.id)
                assigned += 1

            results.append({
                "name": name,
                "type": room_type,
                # PLAIN area_id on purpose: ga_manager turns it into area_ref
                # with its own salt before anything leaves the device.
                "area_id": area.id,
                "created": created,
                "matched_on": matched_on,
                "renamed_by_resident": renamed_by_resident,
                "members_assigned": assigned,
                "members_unknown": unknown,
            })

        state[STATE_KEY] = installed
        await _get_store(hass).async_save(state)

        _LOGGER.info(
            "rooms-sync: %d room(s) — %d created, %d device(s) placed, %d unknown",
            len(results),
            sum(1 for r in results if r["created"]),
            sum(r["members_assigned"] for r in results),
            sum(len(r["members_unknown"]) for r in results),
        )
        return self.json({"rooms": results})
