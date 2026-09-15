"""Tests for ``POST /api/greenautarky_site/rooms/sync`` — KB #184, ADR-0008.

GACI enters the flat's rooms at installation and pushes them here. The two
properties that matter and are easy to get wrong:

* it must MERGE with the rooms the device seeded itself, not duplicate beside
  them — a fresh device already has living_room / kitchen / bedroom, so that is
  the normal case;
* ``area_id`` must come out as the room TYPE even though the displayed name is
  German, because the fleet-wide room type is derived from the id and nothing
  else. Get that wrong and every room silently reads as "custom".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import label_registry as lr

from greenautarky_site.const import DOMAIN
from greenautarky_site.rooms_sync import STATE_KEY, GARoomsSyncView

IEEE_A = "0x00158d000abcd001"
IEEE_B = "0x00158d000abcd002"


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self._body = body

    async def json(self):
        if self._body is _BAD_JSON:
            raise ValueError("not json")
        return self._body


_BAD_JSON = object()


def _seed(hass, state=None):
    st = state if state is not None else {"completed": False}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


def _body(resp) -> dict[str, Any]:
    return json.loads(resp.body)


async def _post(hass, body):
    return await GARoomsSyncView().post(_FakeRequest(hass, body))


def _seed_default_areas(hass) -> None:
    """What a freshly flashed device actually looks like.

    Measured on K31 after a reflash on 2026-08-25: three areas whose ids are the
    English catalogue slugs, created in the same second.
    """
    registry = ar.async_get(hass)
    for name in ("Living Room", "Kitchen", "Bedroom"):
        registry.async_create(name)


def _add_zigbee_device(hass, ieee: str):
    """A device registry entry shaped like a Zigbee2MQTT device."""
    entry = dr.async_get(hass).async_get_or_create(
        config_entry_id="test-entry",
        identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
        name=f"sensor {ieee}",
    )
    return entry


@pytest.fixture
def config_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="test-entry")
    entry.add_to_hass(hass)
    return entry


# ─── the case that happens in every flat ────────────────────────────────


async def test_adopts_the_seeded_default_rooms_instead_of_duplicating_them(hass):
    """The whole point. A fresh device has Living Room / Kitchen / Bedroom; the
    installer types German names. Three rooms must come out, not six."""
    _seed(hass)
    _seed_default_areas(hass)

    resp = await _post(hass, {"rooms": [
        {"name": "Wohnzimmer", "type": "living_room"},
        {"name": "Küche", "type": "kitchen"},
        {"name": "Schlafzimmer", "type": "bedroom"},
    ]})

    assert resp.status == 200
    rooms = _body(resp)["rooms"]
    assert [r["created"] for r in rooms] == [False, False, False]
    assert [r["matched_on"] for r in rooms] == ["id", "id", "id"]

    registry = ar.async_get(hass)
    assert len(registry.async_list_areas()) == 3, "no duplicates"
    assert {a.name for a in registry.async_list_areas()} == {
        "Wohnzimmer", "Küche", "Schlafzimmer"
    }, "the seeded English names must be replaced by the installer's"


async def test_a_new_room_gets_the_type_as_its_area_id(hass):
    """LEGACY CONTRACT, kept green on purpose. Before 2.5.0 `type` was the
    area_id, and a pre-2.5.0 GACI still sends it. It must keep behaving exactly
    as it did — the new `ref`/`kind` split is additive, not a flag day."""
    _seed(hass)
    resp = await _post(hass, {"rooms": [{"name": "Badezimmer", "type": "bathroom"}]})

    room = _body(resp)["rooms"][0]
    assert room["created"] is True
    assert room["area_id"] == "bathroom", "the id must be the TYPE, not the German name"

    area = ar.async_get(hass).async_get_area("bathroom")
    assert area.name == "Badezimmer", "…while the resident sees the German name"


async def test_a_room_without_a_type_still_works(hass):
    """Type is wanted, not required — an older GACI build may not send one."""
    _seed(hass)
    resp = await _post(hass, {"rooms": [{"name": "Hobbyraum"}]})
    room = _body(resp)["rooms"][0]
    assert room["created"] is True and room["type"] is None
    assert ar.async_get(hass).async_get_area(room["area_id"]).name == "Hobbyraum"


async def test_running_it_twice_changes_nothing(hass):
    """It runs again on every follow-up visit, and a reflashed device needs a
    re-sync — so this is the recovery path as well as the install path."""
    _seed(hass)
    payload = {"rooms": [{"name": "Wohnzimmer", "type": "living_room"},
                         {"name": "Küche", "type": "kitchen"}]}
    await _post(hass, payload)
    before = {(a.id, a.name) for a in ar.async_get(hass).async_list_areas()}

    resp = await _post(hass, payload)

    after = {(a.id, a.name) for a in ar.async_get(hass).async_list_areas()}
    assert after == before
    assert all(r["created"] is False for r in _body(resp)["rooms"])


# ─── device placement ───────────────────────────────────────────────────


async def test_devices_are_placed_in_their_room_by_ieee(hass, config_entry):
    _seed(hass)
    dev_a = _add_zigbee_device(hass, IEEE_A)
    dev_b = _add_zigbee_device(hass, IEEE_B)

    resp = await _post(hass, {"rooms": [
        {"name": "Wohnzimmer", "type": "living_room", "members": [IEEE_A, IEEE_B]},
    ]})

    assert _body(resp)["rooms"][0]["members_assigned"] == 2
    registry = dr.async_get(hass)
    assert registry.async_get(dev_a.id).area_id == "living_room"
    assert registry.async_get(dev_b.id).area_id == "living_room"


async def test_ieee_matching_is_case_insensitive(hass, config_entry):
    """Z2M writes lowercase; GACI's database may hold either."""
    _seed(hass)
    dev = _add_zigbee_device(hass, IEEE_A)
    resp = await _post(hass, {"rooms": [
        {"name": "Küche", "type": "kitchen", "members": [IEEE_A.upper()]},
    ]})
    assert _body(resp)["rooms"][0]["members_assigned"] == 1
    assert dr.async_get(hass).async_get(dev.id).area_id == "kitchen"


async def test_an_unknown_sensor_is_reported_not_swallowed(hass):
    """Almost always "not interviewed yet" rather than an error. The caller has
    to be able to retry, so silence here would be the wrong answer."""
    _seed(hass)
    resp = await _post(hass, {"rooms": [
        {"name": "Wohnzimmer", "type": "living_room", "members": [IEEE_A]},
    ]})
    room = _body(resp)["rooms"][0]
    assert room["members_assigned"] == 0
    assert room["members_unknown"] == [IEEE_A]


# ─── the rename signal — one bit, never the name ────────────────────────


async def test_a_resident_rename_is_reported_without_the_new_name(hass):
    """GACI must be able to show "Wohnzimmer (renamed)" rather than presenting a
    stale name as current. The new name is personal data and must not leave."""
    _seed(hass)
    await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})

    ar.async_get(hass).async_update("living_room", name="Papas Zimmer")

    resp = await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})
    room = _body(resp)["rooms"][0]

    assert room["renamed_by_resident"] is True
    assert "Papas Zimmer" not in json.dumps(_body(resp)), "the new name must never leave"


async def test_a_resident_rename_is_not_overwritten(hass):
    """Merge means the resident wins after installation. Re-syncing must not
    silently rename their room back."""
    _seed(hass)
    await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})
    ar.async_get(hass).async_update("living_room", name="Papas Zimmer")

    await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})

    assert ar.async_get(hass).async_get_area("living_room").name == "Papas Zimmer"


async def test_the_installed_name_is_persisted(hass):
    """Without it there is nothing to compare a later name against, and the
    rename signal cannot exist."""
    state = _seed(hass)
    await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})
    assert state[STATE_KEY]["living_room"] == "Wohnzimmer"
    assert hass.data[DOMAIN]["store"].saved is not None, "state must be written, not just held"


# ─── what it refuses ────────────────────────────────────────────────────


async def test_replace_mode_is_refused_not_downgraded(hass):
    """A caller asking for replace believes it can delete rooms. Quietly doing
    something else would leave it believing that."""
    _seed(hass)
    resp = await _post(hass, {"mode": "replace", "rooms": [{"name": "X"}]})
    assert resp.status == 400
    assert "merge is the only mode" in _body(resp)["message"]


@pytest.mark.parametrize("body,fragment", [
    ({"rooms": []}, "non-empty"),
    ({"rooms": "Wohnzimmer"}, "non-empty"),
    ({}, "non-empty"),
    ({"rooms": [{"name": "  "}]}, "needs a name"),
    ({"rooms": ["Wohnzimmer"]}, "must be an object"),
    ({"rooms": [{"name": "X", "type": "Wohn Zimmer"}]}, "not a valid slug"),
    ({"rooms": [{"name": "X", "members": "0xAA"}]}, "members must be a list"),
])
async def test_bad_payloads_are_rejected(hass, body, fragment):
    _seed(hass)
    resp = await _post(hass, body)
    assert resp.status == 400
    assert fragment in _body(resp)["message"]


async def test_non_json_is_rejected(hass):
    _seed(hass)
    resp = await GARoomsSyncView().post(_FakeRequest(hass, _BAD_JSON))
    assert resp.status == 400


async def test_absurd_room_counts_are_capped(hass):
    """A runaway caller must not be able to fill the area registry."""
    _seed(hass)
    resp = await _post(hass, {"rooms": [{"name": f"R{i}"} for i in range(200)]})
    assert resp.status == 400
    assert "exceeds the limit" in _body(resp)["message"]


# ─── nothing personal leaves ────────────────────────────────────────────


async def test_the_response_carries_no_pseudonym(hass):
    """area_ref is sha256(salt + area_id) and the salt lives in ga_manager's
    add-on volume, which this container cannot read. The plain area_id is
    returned here and ga_manager converts it — so a pseudonym appearing in this
    response would mean someone duplicated the salt."""
    _seed(hass)
    resp = await _post(hass, {"rooms": [{"name": "Wohnzimmer", "type": "living_room"}]})
    assert "area_ref" not in _body(resp)["rooms"][0]
    assert "area_id" in _body(resp)["rooms"][0]


# ─── identity split from type (KB #224) ──────────────────────────────────


async def test_two_rooms_of_one_kind_stay_two_rooms(hass, config_entry):
    """THE regression. With `type` as the id, a flat with two bedrooms produced
    ONE area named after the last room, holding both rooms' devices, at HTTP
    200 with no warning. Distinct refs make it expressible."""
    _seed(hass)
    _add_zigbee_device(hass, IEEE_A)
    _add_zigbee_device(hass, IEEE_B)

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a5", "kind": "bedroom",
         "name": "Schlafzimmer Eltern", "members": [IEEE_A]},
        {"ref": "room_1a6", "kind": "bedroom",
         "name": "Schlafzimmer Kind", "members": [IEEE_B]},
    ]})
    assert resp.status == 200
    rooms = _body(resp)["rooms"]
    assert [r["area_id"] for r in rooms] == ["room_1a5", "room_1a6"]

    registry = ar.async_get(hass)
    assert len(registry.async_list_areas()) == 2
    placed = {d.name: d.area_id for d in dr.async_get(hass).devices.values()}
    assert placed[f"sensor {IEEE_A}"] == "room_1a5"
    assert placed[f"sensor {IEEE_B}"] == "room_1a6"


async def test_the_kind_lands_as_a_label_whose_id_ga_manager_can_match(hass):
    """ga_manager matches `label_id` against its pinned catalogue, so the ID is
    the load-bearing half — not the label's display name."""
    _seed(hass)
    await _post(hass, {"rooms": [
        {"ref": "room_1a5", "kind": "bedroom", "name": "Schlafzimmer Eltern"},
    ]})
    area = ar.async_get(hass).async_get_area("room_1a5")
    assert area.labels == {"bedroom"}
    assert lr.async_get(hass).async_get_label("bedroom").name == "Bedroom"


async def test_kind_name_translates_the_label_without_moving_its_id(hass):
    """The resident reads "Schlafzimmer"; ga_manager still matches `bedroom`.
    Same two-step as areas, because labels share the id-generation base class."""
    _seed(hass)
    await _post(hass, {"rooms": [
        {"ref": "room_1a5", "kind": "bedroom", "kind_name": "Schlafzimmer",
         "name": "Schlafzimmer Eltern"},
    ]})
    label = lr.async_get(hass).async_get_label("bedroom")
    assert label.label_id == "bedroom", "the id ga_manager matches on"
    assert label.name == "Schlafzimmer", "what the resident reads"
    assert ar.async_get(hass).async_get_area("room_1a5").labels == {"bedroom"}


async def test_a_room_without_a_kind_gets_no_label(hass):
    """A room GACI could not classify must not invent one."""
    _seed(hass)
    await _post(hass, {"rooms": [{"ref": "room_1a9", "name": "Papas Zimmer"}]})
    assert ar.async_get(hass).async_get_area("room_1a9").labels == set()
    assert list(lr.async_get(hass).async_list_labels()) == []


# ─── the sweep ───────────────────────────────────────────────────────────


async def test_the_first_sync_sweeps_the_empty_rooms_core_seeded(hass):
    """HA Core's own onboarding creates Living Room / Kitchen / Bedroom and that
    cannot be switched off. Ref-only matching never adopts them, so the first
    sync clears them — after creating, so the registry is never empty and
    site_defaults cannot re-seed into the gap."""
    _seed(hass)
    _seed_default_areas(hass)
    assert len(ar.async_get(hass).async_list_areas()) == 3

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "kind": "living_room", "name": "Wohnzimmer"},
    ]})
    assert _body(resp)["swept"] == 3
    assert [(a.id, a.name) for a in ar.async_get(hass).async_list_areas()] == [
        ("room_1a4", "Wohnzimmer")
    ]


async def test_the_sweep_never_touches_a_room_that_holds_a_device(hass, config_entry):
    """The guard that makes it safe rather than a `replace` in disguise."""
    _seed(hass)
    _seed_default_areas(hass)
    device = _add_zigbee_device(hass, IEEE_A)
    dr.async_get(hass).async_update_device(device.id, area_id="kitchen")

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "kind": "living_room", "name": "Wohnzimmer"},
    ]})
    assert _body(resp)["swept"] == 2, "living_room + bedroom, never kitchen"
    assert ar.async_get(hass).async_get_area("kitchen") is not None


async def test_a_later_sync_does_not_sweep_a_room_the_resident_created(hass):
    """The reason the sweep is first-sync-only. Once a resident exists, an
    EMPTY room may be one they just made and have not filled yet."""
    _seed(hass)
    await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})
    ar.async_get(hass).async_create("Hobbyraum")          # resident, still empty

    resp = await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})
    assert _body(resp)["swept"] == 0
    names = {a.name for a in ar.async_get(hass).async_list_areas()}
    assert "Hobbyraum" in names


async def test_a_lost_ref_map_re_adopts_instead_of_duplicating(hass):
    """Self-healing, and the reason the ref IS the area_id rather than a row in
    a mapping table: lose the store, keep the registry, and the rooms are still
    identifiable. A mapping table would have orphaned them and built duplicates
    beside them."""
    _seed(hass)
    await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})

    state = _seed(hass)                                   # store wiped, registry intact
    resp = await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})

    rooms = _body(resp)["rooms"]
    assert rooms[0]["created"] is False and rooms[0]["matched_on"] == "id"
    assert len(ar.async_get(hass).async_list_areas()) == 1
    assert state[STATE_KEY] == {"room_1a4": "Wohnzimmer"}


async def test_a_resident_rename_survives_a_lost_ref_map_check(hass):
    """The re-adopt above must not become a way to overwrite a rename: with the
    map present, the resident's name stands (already covered), and the re-adopt
    only fires when the map is ABSENT."""
    _seed(hass)
    await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})
    ar.async_get(hass).async_update("room_1a4", name="Salon")

    resp = await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})
    assert _body(resp)["rooms"][0]["renamed_by_resident"] is True
    assert ar.async_get(hass).async_get_area("room_1a4").name == "Salon"


# ─── the collision that killed the whole flat ───────────────────────────
#
# Rooms/sync is not the only thing that creates areas. The DEVICE-PLACEMENT
# path creates them too, and it knows nothing about GACI's refs — so its areas
# carry `id = slug(name)`. Ref-only matching (correctly) does not find them,
# the handler falls into its `created` branch, and Home Assistant refuses a
# second area under a name that is already taken. It refuses by RAISING.
#
# Before this section existed, that raise escaped the per-room loop and took
# the whole request with it — including the device placement further down. One
# unmatchable room therefore left EVERY room without a device, which is what a
# heating engine reads to find a valve. The tests below assert the outcome
# (devices carry an `area_id`), not the status code: a 200 with nothing placed
# is precisely the failure being fixed here.


def _seed_placement_areas(hass, *names):
    """Areas as the device-placement path leaves them: ``id = slug(name)``.

    That path has no ref to work with, so the id is the slug of the name the
    resident sees — exactly the state a later rooms/sync has to merge into.
    """
    registry = ar.async_get(hass)
    return [registry.async_create(n) for n in names]


@pytest.mark.parametrize("room", [
    # A ref that HA can use as an id. The create succeeds, the RENAME onto the
    # taken name is what raises.
    {"ref": "room_1a4", "kind": "living_room", "name": "Wohnzimmer"},
    # No ref at all — an older GACI build. The area is created straight under
    # the display name, so the CREATE is what raises. This is the shape seen in
    # the field.
    {"name": "Wohnzimmer"},
], ids=["ref-renames-onto-taken-name", "no-ref-creates-taken-name"])
async def test_an_area_already_holding_the_room_name_is_adopted_not_thrown(
    hass, config_entry, room
):
    """An area whose name IS the room's name is that room — it just carries a
    slug id, because something other than this handler made it. Home Assistant
    fixes an area_id at creation and offers no way to change it, so the ref
    cannot become the id; adopting the area and recording the ref is the only
    answer that is neither a 500 nor a duplicate room."""
    _seed(hass)
    _seed_placement_areas(hass, "Wohnzimmer")
    device = _add_zigbee_device(hass, IEEE_A)

    resp = await _post(hass, {"rooms": [dict(room, members=[IEEE_A])]})

    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True and body["failed"] == 0
    entry = body["rooms"][0]
    assert entry["ok"] is True, entry.get("error")
    assert entry["created"] is False
    assert entry["matched_on"] == "adopted_name"
    assert entry["area_id"] == "wohnzimmer", "the id HA will not let us change"

    registry = ar.async_get(hass)
    assert len(registry.async_list_areas()) == 1, "adopted, not duplicated"

    # THE POINT. A 200 with nothing placed is the failure being fixed.
    assert dr.async_get(hass).async_get(device.id).area_id == "wohnzimmer"


async def test_one_unmatchable_room_must_not_leave_the_whole_flat_unplaced(
    hass, config_entry
):
    """The live blocker, in one test. A batch is a batch: a room that cannot be
    resolved is reported and the rest of the flat is still placed. Before, the
    first raise aborted the request before any device was touched."""
    _seed(hass)
    dev_a = _add_zigbee_device(hass, IEEE_A)
    dev_b = _add_zigbee_device(hass, IEEE_B)

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "name": "Wohnzimmer", "members": [IEEE_A]},
        # Same name, different ref. HA cannot express two areas under one name,
        # so this room genuinely cannot be created — and must fail ALONE.
        {"ref": "room_1a9", "name": "Wohnzimmer"},
        {"ref": "room_1b2", "name": "Küche", "members": [IEEE_B]},
    ]})

    body = _body(resp)
    assert resp.status == 207, "a batch that did not fully apply is not a 200"
    assert body["ok"] is False and body["failed"] == 1

    good, bad, also_good = body["rooms"]
    assert good["ok"] is True and also_good["ok"] is True
    assert bad["ok"] is False
    assert bad["error"], "a failed room must say WHY"
    assert bad["area_id"] is None

    registry = dr.async_get(hass)
    assert registry.async_get(dev_a.id).area_id == "room_1a4"
    assert registry.async_get(dev_b.id).area_id == "room_1b2", \
        "the room AFTER the failure must still be placed"


async def test_an_adopted_room_survives_a_second_sync_unchanged(hass, config_entry):
    """Idempotence is not a nicety here: the sync runs again on every follow-up
    visit and after every reflash. The second run must find the adopted room by
    its recorded ref — not collide with it again."""
    _seed(hass)
    _seed_placement_areas(hass, "Wohnzimmer")
    device = _add_zigbee_device(hass, IEEE_A)
    payload = {"rooms": [
        {"ref": "room_1a4", "kind": "living_room", "name": "Wohnzimmer",
         "members": [IEEE_A]},
    ]}

    first = await _post(hass, payload)
    before = {(a.id, a.name, frozenset(a.aliases))
              for a in ar.async_get(hass).async_list_areas()}

    second = await _post(hass, payload)

    assert (first.status, second.status) == (200, 200)
    assert _body(second)["ok"] is True
    assert _body(second)["rooms"][0]["matched_on"] == "ref_alias", \
        "found by the ref that was written down, not by its name again"
    after = {(a.id, a.name, frozenset(a.aliases))
             for a in ar.async_get(hass).async_list_areas()}
    assert after == before, "running it twice must change nothing"
    assert dr.async_get(hass).async_get(device.id).area_id == "wohnzimmer"


# ─── the re-sync that accused itself ────────────────────────────────────
#
# THE SHAPE THE FIELD ACTUALLY HAS, which none of the tests above had.
#
# `test_running_it_twice_changes_nothing` uses the pre-2.5.0 `type` field,
# which means ref AND kind at once: the area is then created under an id
# derived from the ref, so the second sync finds it by id and never reaches
# the name guard. `test_an_adopted_room_survives_a_second_sync_unchanged`
# starts from an EMPTY ref map, so its first sync adopts the area and writes
# the ref alias — and its second sync matches on that alias.
#
# A flat installed under 2.3.x/2.4.x has neither property. `_find_area` matched
# BY NAME back then (see the docstring that still explains why that went), so
# the ref map was written under the areas' NAME-derived ids and no alias was
# ever recorded — `_remember_ref` did not exist until 2.7.x. Upgrade that
# device and every room is: not findable by ref, not findable by alias, held by
# an area whose id is in the ref map. Which is precisely the guard's input.


def _flat_installed_by_an_older_build(hass):
    """A flat GACI installed before ref-only matching, as it is on disk.

    Areas under name-derived ids carrying NO alias, and a ref map from the
    earlier syncs keyed on those same ids. Measured on a canary on 2026-09-15.
    """
    state = _seed(hass, {"completed": True, STATE_KEY: {
        "wohnzimmer": "Wohnzimmer",
        "schlafzimmer": "Schlafzimmer",
        "office": "Office",
    }})
    _seed_placement_areas(hass, "Wohnzimmer", "Schlafzimmer", "Office")
    assert all(a.aliases == set() for a in ar.async_get(hass).async_list_areas()), \
        "the whole point: the older build recorded no ref anywhere"
    return state


_OLDER_BUILD_PAYLOAD = {"rooms": [
    {"name": "Wohnzimmer", "ref": "room_9211b00fd8ce43d19c47270595cea31f",
     "kind": "living_room", "members": [IEEE_A]},
    {"name": "Schlafzimmer", "ref": "room_53fa3183d89540b2813cc238c862a5eb",
     "kind": "bedroom", "members": [IEEE_B]},
    {"name": "Office", "ref": "room_7c1d9e2ab4f04f6b8e5a0c3d71286f45",
     "kind": "office"},
]}


async def test_a_resync_of_an_installed_flat_does_not_collide_with_itself(
    hass, config_entry
):
    """3 of 3 rooms FAILED on every re-sync, and two of four thermostats never
    reached a room — the heating engine then has no valve to work with.

    Each room was refused by the guard that asks whether the area holding its
    name is `claimed`, because `claimed` was seeded from the ref map of EARLIER
    syncs. So every room collided with the area it had installed itself, and
    the error said "belongs to another room in this sync", which was not true
    of any of them.
    """
    _flat_installed_by_an_older_build(hass)
    dev_a = _add_zigbee_device(hass, IEEE_A)
    dev_b = _add_zigbee_device(hass, IEEE_B)

    resp = await _post(hass, _OLDER_BUILD_PAYLOAD)

    body = _body(resp)
    failed = [(r["name"], r["error"]) for r in body["rooms"] if r["ok"] is False]
    assert not failed, f"a re-sync collided with its own rooms: {failed}"
    assert resp.status == 200 and body["ok"] is True
    assert [r["matched_on"] for r in body["rooms"]] == ["adopted_name"] * 3
    assert len(ar.async_get(hass).async_list_areas()) == 3, "adopted, not duplicated"

    # THE OUTCOME, not the status code: the valves are in their rooms.
    registry = dr.async_get(hass)
    assert registry.async_get(dev_a.id).area_id == "wohnzimmer"
    assert registry.async_get(dev_b.id).area_id == "schlafzimmer"


async def test_the_resync_heals_the_flat_rather_than_re_adopting_forever(hass):
    """Adoption is not the steady state, it is the repair: it writes the ref
    down as an alias, so the NEXT sync finds the room by ref like any other.

    This is also why the old guard was self-perpetuating — it raised before
    `_remember_ref` could run, so the very state that triggered it was never
    repaired and every sync failed the same way again.
    """
    _flat_installed_by_an_older_build(hass)
    await _post(hass, _OLDER_BUILD_PAYLOAD)

    area = ar.async_get(hass).async_get_area("wohnzimmer")
    assert "room_9211b00fd8ce43d19c47270595cea31f" in area.aliases

    second = _body(await _post(hass, _OLDER_BUILD_PAYLOAD))

    assert second["ok"] is True
    assert [r["matched_on"] for r in second["rooms"]] == ["ref_alias"] * 3
    assert len(ar.async_get(hass).async_list_areas()) == 3


async def test_two_payload_rooms_under_one_name_are_named_for_what_they_are(hass):
    """The guard still has a job — two rooms in ONE request asking for one name
    is a real conflict, and HA cannot express it. What changes is that the
    message now describes the state it found: which request, which room took
    the name, and the fact that it is this one.

    The message travels off the device, so it may carry only values GACI itself
    sent: the room name it chose and the opaque ref of the room that took it.
    An area_id stays in the log — for a resident-made room the id IS their room
    name (see `test_a_failure_report_never_carries_a_resident_chosen_name`).
    """
    _seed(hass)

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "name": "Wohnzimmer"},
        {"ref": "room_1a9", "name": "Wohnzimmer"},
    ]})

    bad = _body(resp)["rooms"][1]
    assert bad["ok"] is False
    error = bad["error"]
    assert "room_1a4" in error, "name the room that actually took the name"
    assert "this request" in error.lower()
    assert "another room in this sync" not in error, \
        "the sentence that was false on every re-sync of an installed flat"

async def test_the_recorded_ref_outlives_this_component_s_store(hass):
    """WHY AN ALIAS AND NOT A ROW IN THE STORE. Home Assistant will not change
    an area_id, so the ref has to be written down somewhere. The alias sits in
    `core.area_registry` next to the id, so it survives what the store does
    not — and the module's own rule holds: lose the store, keep the registry,
    and a re-sync still finds its rooms instead of duplicating them."""
    _seed(hass)
    _seed_placement_areas(hass, "Wohnzimmer")
    payload = {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]}
    await _post(hass, payload)

    area = ar.async_get(hass).async_get_area("wohnzimmer")
    assert "room_1a4" in area.aliases

    _seed(hass)                              # store wiped, registry intact
    resp = await _post(hass, payload)

    assert resp.status == 200
    assert _body(resp)["rooms"][0]["matched_on"] == "ref_alias"
    assert len(ar.async_get(hass).async_list_areas()) == 1


async def test_a_ref_home_assistant_cannot_use_as_an_id_is_found_next_time(hass):
    """The sibling the code already predicted in a comment and then left
    broken: HA slugifies the seed name, so a ref that does not survive that
    round-trip (`room__1a4` -> id `room_1a4`) produced a room its own ref could
    not address. The old code logged a warning and moved on — the NEXT sync
    then built a second room beside it, or raised on the name. Now the ref is
    recorded the same way an adopted room's is."""
    _seed(hass)
    first = await _post(hass, {"rooms": [{"ref": "room__1a4", "name": "Wohnzimmer"}]})

    area_id = _body(first)["rooms"][0]["area_id"]
    assert area_id == "room_1a4", "HA dropped the doubled underscore"
    assert "room__1a4" in ar.async_get(hass).async_get_area(area_id).aliases

    second = await _post(hass, {"rooms": [{"ref": "room__1a4", "name": "Wohnzimmer"}]})

    assert second.status == 200
    assert _body(second)["rooms"][0]["matched_on"] == "ref_alias"
    assert len(ar.async_get(hass).async_list_areas()) == 1, "one room, not two"


async def test_a_batch_with_a_failure_in_it_sweeps_nothing(hass):
    """A failed room claims no area, so an area that should have been claimed
    looks unclaimed — and the sweep deletes unclaimed empty areas. Deleting on
    a picture we know is incomplete is the one thing the sweep's safety
    properties exist to prevent."""
    _seed(hass)
    _seed_default_areas(hass)                # Living Room / Kitchen / Bedroom

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "name": "Wohnzimmer"},
        {"ref": "room_1a9", "name": "Wohnzimmer"},     # cannot be created
    ]})

    body = _body(resp)
    assert body["failed"] == 1
    assert body["swept"] == 0
    names = {a.name for a in ar.async_get(hass).async_list_areas()}
    assert {"Living Room", "Kitchen", "Bedroom"} <= names


async def test_a_failure_report_never_carries_a_resident_chosen_name(hass):
    """The new `error` field travels off the device, so it is held to the same
    rule as everything else here: an area_id is the resident's own room name
    for a room they made, so a failure says how many, never which."""
    _seed(hass)
    registry = ar.async_get(hass)
    for personal in ("Papas Zimmer", "Mamas Zimmer"):
        area = registry.async_create(personal)
        registry.async_update(area.id, aliases={"room_1a4"})

    resp = await _post(hass, {"rooms": [{"ref": "room_1a4", "name": "Wohnzimmer"}]})

    body = _body(resp)
    assert resp.status == 207 and body["failed"] == 1
    assert "ambiguous" in body["rooms"][0]["error"]
    blob = json.dumps(body)
    for personal in ("Papas Zimmer", "Mamas Zimmer", "papas_zimmer", "mamas_zimmer"):
        assert personal not in blob, "a resident's room name must not leave"


async def test_a_clean_sync_still_answers_200_and_ok(hass, config_entry):
    """The known-good half: nothing above may turn the ordinary path amber."""
    _seed(hass)
    device = _add_zigbee_device(hass, IEEE_A)

    resp = await _post(hass, {"rooms": [
        {"ref": "room_1a4", "kind": "living_room", "name": "Wohnzimmer",
         "members": [IEEE_A]},
    ]})

    body = _body(resp)
    assert resp.status == 200
    assert body["ok"] is True and body["failed"] == 0
    assert body["rooms"][0]["ok"] is True and body["rooms"][0]["error"] is None
    assert dr.async_get(hass).async_get(device.id).area_id == "room_1a4"
