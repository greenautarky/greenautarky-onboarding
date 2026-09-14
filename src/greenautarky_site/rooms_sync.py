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

IDENTITY AND TYPE ARE TWO FIELDS, NOT ONE
-----------------------------------------
``ref`` identifies the room, ``kind`` classifies it. They used to be one field
(``type``), used verbatim as the ``area_id`` — which cannot express a flat with
two bedrooms: the second room matched the first one by id and swallowed it,
devices and all. So:

``ref``   a stable, opaque, never-reused key from GACI's own database. It
          becomes the ``area_id`` wherever this handler creates the area, and
          because HA fixes an id at creation and keeps it through every rename,
          ``ref == area_id`` from then on. That makes the ref map a cache
          rather than a source of truth: lose it and a re-sync still finds its
          rooms.

          Where the area ALREADY existed — device placement creates areas too,
          under ``id = slug(name)`` — the id cannot be moved onto the ref, so
          the ref is recorded as an ALIAS on that area instead (see
          ``_remember_ref``). Identity is still the ref and still lives in the
          registry rather than in this component's store; it is just read from
          one of two places.
``kind``  a catalogue room type, written as an HA LABEL. The resident may
          remove it; ga_manager then reports ``area_kind=custom``, which is the
          honest answer. The pinned catalogue itself stays in ga_manager
          (``ha_areas.CATALOGUE``) — one source of truth, not copied here.

``type`` is still accepted from older GACI builds and means ref AND kind at
once, which is exactly what it did before. Nothing that works today breaks.

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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from .store import _get_state, _get_store

_LOGGER = logging.getLogger(__name__)

# A ref becomes an area_id and a kind becomes a label_id, so both must look
# like a slug HA would have produced itself.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Zigbee IEEE address as it appears in a device registry identifier.
_IEEE_RE = re.compile(r"0x[0-9a-fA-F]{16}")

MAX_ROOMS = 64
MAX_MEMBERS_PER_ROOM = 128

# state key: {area_id: name GACI installed}. Lets a later sync report that the
# resident has renamed a room WITHOUT the new name ever leaving the device.
STATE_KEY = "rooms_sync_installed_names"


def _norm(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def _seed_name(slug: str) -> str:
    """The name whose slug IS the slug — "room_1a4" -> "Room 1A4".

    Home Assistant will not let a caller choose an id: it slugifies whatever
    name it is given. So the object is created under a name that slugifies to
    the value we want, then renamed. The id is fixed at creation and survives
    every rename, so both properties hold at once — the machine keeps the slug
    it needs, the human sees the word they chose.

    Used for AREAS (seed "Room 1A4" -> id `room_1a4` -> rename "Wohnzimmer")
    and for LABELS alike (seed "Bedroom" -> id `bedroom` -> rename
    "Schlafzimmer"), because both registries share the id-generation base class.
    """
    return slug.replace("_", " ").title()


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


def _find_area(registry: ar.AreaRegistry, ref: str | None):
    """Match an existing area BY REF ONLY. No name fallback.

    The name fallback used to be how the device-seeded defaults were adopted,
    back when the id was the room type and therefore predictable. With an
    opaque ref it does the opposite of its job: it matches on whatever the
    installer happened to type, so a room's identity depends on whether that
    text collides with a seeded name. Measured against the seeded defaults, a
    two-room sync produced one clean room, one room stuck on the old id, and
    two orphans.

    The seeded defaults are handled where they belong instead — see
    ``_sweep_unclaimed`` — and identity now comes from one place only.

    TWO REF LOOKUPS, STILL ONE IDENTITY
    -----------------------------------
    ``ref == area_id`` is the normal case and stays first. The second lookup is
    the ALIAS, and it is not the name fallback wearing a hat: an alias is only
    ever on an area because a previous run of THIS handler wrote it there (see
    ``_remember_ref``) for a room whose area already existed under an id Home
    Assistant will not change. It is a mapping this code recorded, keyed on the
    ref — not a guess at what the installer typed — so it cannot make a room's
    identity depend on its display name, which is the whole reason the name
    fallback went.

    Raises ValueError when the ref is recorded on more than one area: that is
    ambiguous rather than merely unknown, and guessing is how the removed
    fallback produced orphans.
    """
    if not ref:
        return None, None
    existing = registry.async_get_area(ref)
    if existing is not None:
        return existing, "id"
    by_alias = list(registry.async_get_areas_by_alias(ref))
    if len(by_alias) > 1:
        # Ids stay out of the message — for a resident-created room the id IS
        # their room name, and this string travels off the device.
        _LOGGER.error("rooms-sync: ref %r is on areas %s", ref,
                      ", ".join(sorted(a.id for a in by_alias)))
        raise ValueError(
            f"ref {ref!r} is recorded on {len(by_alias)} areas — ambiguous"
        )
    if by_alias:
        return by_alias[0], "ref_alias"
    return None, None


def _remember_ref(registry: ar.AreaRegistry, area, ref: str | None):
    """Write ``ref`` onto ``area`` as an alias, when the id could not carry it.

    Home Assistant fixes an ``area_id`` at creation and offers no way to change
    it — ``async_update`` has no ``id`` parameter — so an area that already
    exists under a slug id can never be re-keyed onto GACI's ref. Without a
    second place to write the ref down, every later sync looks for a room it
    cannot find by id and builds a duplicate beside it. That is not a
    hypothetical: the ``created`` branch below used to WARN about exactly this
    and then leave it broken.

    The alias registry is that second place, and it is the right one. It lives
    in ``core.area_registry`` next to the id, so it survives a restart and a
    reset of this component's store; Home Assistant indexes it
    (``async_get_areas_by_alias``) using its OWN name normalisation, so no copy
    of that rule lives here; and aliases carry no uniqueness constraint, so
    recording one cannot raise.

    A no-op when the id already IS the ref, which is the normal case — a room
    that does not need the alias does not get one.
    """
    if not ref or area.id == ref or ref in area.aliases:
        return area
    _LOGGER.info(
        "rooms-sync: area %s cannot carry ref %r as its id (ids are immutable) "
        "— recording the ref as an alias so the next sync finds this room",
        area.id, ref,
    )
    return registry.async_update(area.id, aliases=set(area.aliases) | {ref})


def _rename(registry: ar.AreaRegistry, area, name: str):
    """Rename ``area`` to ``name``; keep the room when the name is taken.

    ``async_update`` enforces the same unique-name rule as ``async_create`` and
    raises the same ValueError. A display name is the least load-bearing thing
    about a room — the id, and the devices sitting in it, are what the heating
    engine reads — so a name clash must cost the name, never the room.

    Returns ``(area, conflict)``; ``conflict`` is None when the rename stuck.
    """
    if area.name == name:
        return area, None
    try:
        return registry.async_update(area.id, name=name), None
    except ValueError as exc:
        _LOGGER.warning(
            "rooms-sync: area %s keeps its current name (%s)", area.id, exc)
        return area, str(exc)


def _ensure_kind_label(hass: HomeAssistant, kind: str) -> str | None:
    """The label whose ``label_id`` is ``kind``, created if it is missing.

    ga_manager matches ``label_id`` against its pinned catalogue, so the id is
    the load-bearing half; the label's NAME is only what the resident reads and
    may be given in their language via ``kind_name``.

    Returns None if HA would not give us the id we need — the room is still
    correct, it simply reports as ``custom`` until someone labels it by hand.
    """
    registry = lr.async_get(hass)
    existing = registry.async_get_label(kind)
    if existing is not None:
        return existing.label_id
    try:
        label = registry.async_create(_seed_name(kind))
    except ValueError as exc:
        # A label already carries that display name under a different id.
        _LOGGER.warning("rooms-sync: cannot create label %r (%s)", kind, exc)
        return None
    if label.label_id != kind:
        _LOGGER.warning(
            "rooms-sync: wanted label_id %r but Home Assistant assigned %r — "
            "rooms carrying it will report as 'custom'", kind, label.label_id,
        )
        return None
    return label.label_id


def _sweep_unclaimed(hass: HomeAssistant, claimed: set[str]) -> list[str]:
    """Delete areas nobody is using — FIRST SYNC ONLY. Returns what went.

    Home Assistant's own onboarding creates Living Room / Kitchen / Bedroom
    when the owner account is made (``components/onboarding/views.py``), and
    that cannot be switched off. Under ref-only matching they are never
    adopted, so without this they linger forever as empty English rooms in a
    German product.

    Three properties make this safe, and it is NOT ``replace``:

    * it runs only on the first sync, when the ref map is still empty — at that
      moment no resident exists, so nothing empty can be theirs;
    * it only ever touches areas holding no device and no entity;
    * it runs AFTER the new rooms exist, so the registry is never empty and
      ``site_defaults`` cannot re-seed into the gap.

    The same conditions hold after a reflash, which is why the recovery path
    cleans up too.
    """
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    swept: list[str] = []
    for area in list(area_reg.async_list_areas()):
        if area.id in claimed:
            continue
        if dr.async_entries_for_area(dev_reg, area.id):
            continue
        if er.async_entries_for_area(ent_reg, area.id):
            continue
        area_reg.async_delete(area.id)
        swept.append(area.id)
    if swept:
        _LOGGER.info("rooms-sync: swept %d unclaimed empty area(s): %s",
                     len(swept), ", ".join(swept))
    return swept


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
        # Empty ref map == nobody has ever synced this device. That is the only
        # moment the sweep may run; see _sweep_unclaimed.
        first_sync = not installed
        claimed: set[str] = set(installed)

        results: list[dict[str, Any]] = []
        failures = 0
        for entry in rooms:
            if not isinstance(entry, dict):
                return self.json({"message": "each room must be an object"}, status_code=400)

            name = str(entry.get("name") or "").strip()
            if not name:
                return self.json({"message": "every room needs a name"}, status_code=400)

            # `type` is the pre-2.5.0 field and meant ref AND kind at once.
            # Reading it as both is exactly what it used to do, so an older
            # GACI build keeps behaving identically.
            legacy = entry.get("type")
            ref = entry.get("ref", legacy)
            kind = entry.get("kind", legacy if "ref" not in entry else None)
            for field, value in (("ref", ref), ("kind", kind)):
                if value is None:
                    continue
                value = str(value).strip().lower()
                if not _SLUG_RE.match(value):
                    return self.json(
                        {"message": f"room {field} {value!r} is not a valid slug"},
                        status_code=400,
                    )
                if field == "ref":
                    ref = value
                else:
                    kind = value
            kind_name = str(entry.get("kind_name") or "").strip() or None

            members = entry.get("members") or []
            if not isinstance(members, list):
                return self.json({"message": "members must be a list"}, status_code=400)
            if len(members) > MAX_MEMBERS_PER_ROOM:
                return self.json(
                    {"message": f"room {name!r} has more than "
                                f"{MAX_MEMBERS_PER_ROOM} members"},
                    status_code=400,
                )

            # ONE ROOM MUST NOT FAIL THE FLAT. Everything below touches the
            # registries, and every registry write can raise. Without this try
            # block a single unmatchable room aborted the request at the first
            # raise — BEFORE the device placement further down — so the heating
            # engine found no valve in ANY room, because of one.
            try:
                area, matched_on = _find_area(area_reg, ref)
                created = area is None
                name_conflict = None

                if created:
                    # Home Assistant refuses a second area with the same
                    # normalised name and RAISES rather than returning, so ask
                    # first — with HA's own lookup, the very function its guard
                    # calls, so this answer cannot drift from the one it gives.
                    #
                    # This is not the name fallback `_find_area` dropped. It
                    # runs only after ref matching has already failed, and only
                    # over the set where the alternative is not "a separate
                    # room" but a raised ValueError: HA will not create a room
                    # under a name that is taken, now or ever. An area whose
                    # name IS the room's name is that room — it simply carries
                    # a slug id, because something other than this handler
                    # created it (device placement does).
                    holder = area_reg.async_get_area_by_name(name)
                    if holder is not None:
                        if holder.id in claimed:
                            # Two rooms cannot share a name in HA, so this is a
                            # real conflict in the payload — not something to
                            # settle by taking the room off the first one.
                            raise ValueError(
                                "the area already holding this name belongs to "
                                "another room in this sync"
                            )
                        area = _remember_ref(area_reg, holder, ref)
                        created, matched_on = False, "adopted_name"
                    else:
                        # Seeded under a name that slugifies to the ref, then
                        # renamed.
                        area = area_reg.async_create(_seed_name(ref) if ref else name)
                        area, name_conflict = _rename(area_reg, area, name)
                        # HA appends `_2` when the id it derives is taken, so
                        # the ref may not address the room. That used to be a
                        # warning about corruption it then left in place; now
                        # the ref is written where the next sync looks.
                        area = _remember_ref(area_reg, area, ref)

                renamed_by_resident = False
                if not created:
                    previous = installed.get(area.id)
                    if previous is not None and _norm(previous) != _norm(area.name):
                        # The resident renamed it. Report THAT, never the new
                        # name — one bit, no content.
                        renamed_by_resident = True
                    elif previous is None and _norm(area.name) != _norm(name):
                        # Our own area, but the ref map is gone — the store was
                        # reset while the registry survived. Because ref ==
                        # area_id the room is still identifiable, so re-adopt it
                        # rather than building a duplicate next to it. This is
                        # the self-healing half of using the ref as the id.
                        area, name_conflict = _rename(area_reg, area, name)

                # The kind rides as a LABEL, so the id stays pure identity.
                kind_applied = None
                if kind:
                    label_id = _ensure_kind_label(hass, kind)
                    if label_id is not None:
                        if kind_name and (lab := lr.async_get(hass).async_get_label(label_id)) \
                                and lab.name != kind_name:
                            try:
                                lr.async_get(hass).async_update(label_id, name=kind_name)
                            except ValueError as exc:
                                # Same unique-name rule as areas. A label's
                                # display name is cosmetic; the room is not.
                                _LOGGER.warning(
                                    "rooms-sync: label %s keeps its current name (%s)",
                                    label_id, exc)
                        if label_id not in area.labels:
                            area = area_reg.async_update(
                                area.id, labels=set(area.labels) | {label_id})
                        kind_applied = label_id

                installed[area.id] = name
                claimed.add(area.id)

                assigned, unknown = 0, []
                for raw in members:
                    ieee = str(raw).strip().lower()
                    device_id = by_ieee.get(ieee)
                    if device_id is None:
                        # Almost always "not interviewed yet" rather than an
                        # error: the sensor has not appeared in the device
                        # registry. Named so the caller can retry rather than
                        # assuming success.
                        unknown.append(ieee)
                        continue
                    dev_reg.async_update_device(device_id, area_id=area.id)
                    assigned += 1

                results.append({
                    "name": name,
                    "ref": ref,
                    "kind": kind_applied,
                    # Kept so a pre-2.5.0 GACI reads what it always read.
                    "type": ref,
                    # PLAIN area_id on purpose: ga_manager turns it into
                    # area_ref with its own salt before anything leaves the
                    # device.
                    "area_id": area.id,
                    "ok": True,
                    "error": None,
                    "created": created,
                    "matched_on": matched_on,
                    "name_conflict": name_conflict,
                    "renamed_by_resident": renamed_by_resident,
                    "members_assigned": assigned,
                    "members_unknown": unknown,
                })
            except Exception as exc:  # broad ON PURPOSE — reported, not swallowed
                # Counted and named per room, so the answer can never read as a
                # bare success over a batch that did not fully apply.
                failures += 1
                _LOGGER.error("rooms-sync: room %r (ref %r) failed: %s",
                              name, ref, exc, exc_info=True)
                results.append({
                    "name": name,
                    "ref": ref,
                    "kind": None,
                    "type": ref,
                    "area_id": None,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "created": False,
                    "matched_on": None,
                    "name_conflict": None,
                    "renamed_by_resident": False,
                    "members_assigned": 0,
                    "members_unknown": [],
                })

        # A failed room claimed no area, so an area that SHOULD have been
        # claimed can look unclaimed. Deleting on that picture is exactly the
        # mistake the sweep's three safety properties exist to prevent, so a
        # batch with any failure in it sweeps nothing.
        swept = _sweep_unclaimed(hass, claimed) if first_sync and not failures else []

        state[STATE_KEY] = installed
        await _get_store(hass).async_save(state)

        _LOGGER.info(
            "rooms-sync: %d room(s) — %d created, %d device(s) placed, %d unknown, "
            "%d unclaimed area(s) swept, %d room(s) FAILED",
            len(results),
            sum(1 for r in results if r["created"]),
            sum(r["members_assigned"] for r in results),
            sum(len(r["members_unknown"]) for r in results),
            len(swept),
            failures,
        )
        # `swept` carries plain area_ids, which for a resident-created room is
        # their room name. It stays a COUNT on the wire.
        payload = {
            "rooms": results,
            "swept": len(swept),
            "ok": failures == 0,
            "failed": failures,
        }
        # 207 rather than 200 when a room failed: a caller that checks for 200
        # sees the difference, and one that only checks 2xx still finds `ok`
        # false and a non-zero `failed` in the body it already parses. What
        # must not happen is a bare 200 over a batch that did not fully apply —
        # the rooms that DID apply are applied, and that is a partial success,
        # not a success.
        return self.json(payload, status_code=200 if not failures else 207)
