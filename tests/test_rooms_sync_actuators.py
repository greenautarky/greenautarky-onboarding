"""Underfloor relay channels in ``POST /api/greenautarky_site/rooms/sync`` (contract v1).

Contract: ga-ihost-docs ``docs/heating-ufh-relay-channel-assignment-concept.md`` §10,
Odoo #1482, Thomas' decisions 2026-11-19 (D4 release rule, D7 warn-not-block).

A relay is ONE device feeding SEVERAL rooms. Its channels are placed at ENTITY level
(area + label ``ga_heat_actuator`` + hidden) — the shape ga_heating 0.14.4 drives — and
the device itself is never given an area.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from greenautarky_site.const import DOMAIN
from greenautarky_site.rooms_sync import (
    ACTUATOR_LABEL,
    ACTUATOR_STATE_KEY,
    GARoomsSyncView,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rooms_sync_actuators.v1.json"
#: The SAME bytes as ga-ihost-docs docs/contracts/fixtures/ and the producer/transport
#: repos — a drifted copy would test a contract nobody else speaks.
FIXTURE_SHA256 = "422940fd1cca0e8f2b0865ad4e10423f4775bb3ad22594fe722cca9a2152d6af"
RELAY = "0xa4c1382ee485666c"
MEMBERS = ("0xaaaaaaaaaaaaaaa1", "0xaaaaaaaaaaaaaaa2")


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _Req:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def config_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="test-entry")
    entry.add_to_hass(hass)
    return entry


def _seed(hass, state=None) -> dict[str, Any]:
    st = state if state is not None else {"completed": False}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": st}
    return st


def _device(hass, ieee: str):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id="test-entry",
        identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
        name=ieee,
    )


def _relay(hass, ieee: str = RELAY, *, channels=("l1", "l2", "l3", "l4"),
           countdown: bool = True):
    dev = _device(hass, ieee)
    ent = er.async_get(hass)
    for ch in channels:
        ent.async_get_or_create("switch", "mqtt", f"{ieee}_switch_{ch}_zigbee2mqtt",
                                device_id=dev.id, config_entry=None,
                                suggested_object_id=f"{ieee}_{ch}")
        if countdown:
            ent.async_get_or_create("number", "mqtt", f"{ieee}_countdown_{ch}_zigbee2mqtt",
                                    device_id=dev.id, suggested_object_id=f"{ieee}_countdown_{ch}")
    return dev


def _sw(hass, ch: str, ieee: str = RELAY):
    ent = er.async_get(hass)
    return ent.async_get(ent.async_get_entity_id("switch", "mqtt",
                                                 f"{ieee}_switch_{ch}_zigbee2mqtt"))


def _cd(hass, ch: str, ieee: str = RELAY):
    ent = er.async_get(hass)
    eid = ent.async_get_entity_id("number", "mqtt", f"{ieee}_countdown_{ch}_zigbee2mqtt")
    return ent.async_get(eid) if eid else None


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


async def _post(hass, body):
    resp = await GARoomsSyncView().post(_Req(hass, body))
    return resp.status, json.loads(resp.body)


def _by_ref(body) -> dict[str, dict]:
    return {r["ref"]: r for r in body["rooms"]}


# ─── the seam ────────────────────────────────────────────────────────────


def test_the_fixture_is_the_shared_contract_bytes():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_label_is_the_one_ga_heating_drives():
    """Pinned literal, not read from ga_heating: a typo on either side must show."""
    assert ACTUATOR_LABEL == "ga_heat_actuator"


async def test_fixture_request_places_every_channel_at_entity_level(hass, config_entry):
    _seed(hass)
    relay = _relay(hass)
    for m in MEMBERS:
        _device(hass, m)
    fx = _fixture()
    status, body = await _post(hass, fx["request"])
    assert status == 200, body
    rooms = _by_ref(body)
    for ref, want in fx["expected_result"]["rooms"].items():
        assert rooms[ref]["actuators_assigned"] == want["actuators_assigned"]
        assert rooms[ref]["actuators_unknown"] == want["actuators_unknown"]
    assert body["actuators_released"] == fx["expected_result"]["actuators_released"]

    area_of = {r["ref"]: r["area_id"] for r in body["rooms"]}
    for ch, ref in (("l1", "room_11111111111111111111111111111111"),
                    ("l2", "room_22222222222222222222222222222222"),
                    ("l3", "room_22222222222222222222222222222222")):
        sw, cd = _sw(hass, ch), _cd(hass, ch)
        assert sw.area_id == area_of[ref]
        assert ACTUATOR_LABEL in sw.labels
        assert sw.hidden_by is er.RegistryEntryHider.INTEGRATION
        assert cd.area_id == area_of[ref] and cd.hidden_by is er.RegistryEntryHider.INTEGRATION
        assert ACTUATOR_LABEL not in cd.labels
    # Unused channel l4 is untouched, and the relay DEVICE has no room.
    assert _sw(hass, "l4").area_id is None and ACTUATOR_LABEL not in _sw(hass, "l4").labels
    assert dr.async_get(hass).async_get(relay.id).area_id is None
    assert lr.async_get(hass).async_get_label(ACTUATOR_LABEL) is not None


# ─── D4: release ─────────────────────────────────────────────────────────


async def test_a_channel_no_longer_listed_is_released_and_stays_hidden(hass, config_entry):
    st = _seed(hass)
    _relay(hass)
    fx = _fixture()
    await _post(hass, fx["request"])
    second = json.loads(json.dumps(fx["request"]))
    second["rooms"][1]["actuators"] = [{"ieee": RELAY, "channel": "l2"}]   # l3 dropped
    status, body = await _post(hass, second)
    assert status == 200
    assert body["actuators_released"] == [f"{RELAY}/l3"]
    sw, cd = _sw(hass, "l3"), _cd(hass, "l3")
    assert ACTUATOR_LABEL not in sw.labels and sw.area_id is None
    assert sw.hidden_by is er.RegistryEntryHider.INTEGRATION
    assert cd.area_id is None and cd.hidden_by is er.RegistryEntryHider.INTEGRATION
    assert ACTUATOR_LABEL in _sw(hass, "l2").labels                  # kept
    assert set(st[ACTUATOR_STATE_KEY]) == {f"{RELAY}/l1", f"{RELAY}/l2"}


async def test_an_older_producer_without_the_key_places_and_releases_nothing(
    hass, config_entry
):
    st = _seed(hass)
    _relay(hass)
    await _post(hass, _fixture()["request"])
    old = {"rooms": [{k: v for k, v in r.items() if k != "actuators"}
                     for r in _fixture()["request"]["rooms"]]}
    status, body = await _post(hass, old)
    assert status == 200 and body["actuators_released"] == []
    assert ACTUATOR_LABEL in _sw(hass, "l1").labels
    assert len(st[ACTUATOR_STATE_KEY]) == 3


async def test_only_channels_this_handler_placed_are_ever_released(hass, config_entry):
    """A channel labelled by someone else (e.g. by hand on a bench) is not ours."""
    _seed(hass)
    _relay(hass)
    ent = er.async_get(hass)
    lr.async_get(hass).async_create("ga_heat_actuator")
    ent.async_update_entity(_sw(hass, "l4").entity_id, labels={ACTUATOR_LABEL})
    status, body = await _post(hass, _fixture()["request"])
    assert status == 200 and body["actuators_released"] == []
    assert ACTUATOR_LABEL in _sw(hass, "l4").labels


async def test_a_failed_room_releases_nothing(hass, config_entry, monkeypatch):
    """Same safety rule as the sweep: a failed room's channels look unlisted."""
    _seed(hass)
    _relay(hass)
    await _post(hass, _fixture()["request"])
    import greenautarky_site.rooms_sync as rs

    real = rs._place_actuator

    def boom(hass_, ent_reg, area_id, label_id, by_ieee, ieee, channel):
        if channel == "l2":
            raise RuntimeError("registry write refused")
        return real(hass_, ent_reg, area_id, label_id, by_ieee, ieee, channel)

    monkeypatch.setattr(rs, "_place_actuator", boom)
    second = json.loads(json.dumps(_fixture()["request"]))
    second["rooms"][0]["actuators"] = []                              # l1 would be released
    status, body = await _post(hass, second)
    assert status == 207 and body["failed"] == 1
    assert body["actuators_released"] == []
    assert ACTUATOR_LABEL in _sw(hass, "l1").labels


# ─── reported, not silent ────────────────────────────────────────────────


async def test_unknown_relay_and_unknown_channel_are_reported(hass, config_entry):
    _seed(hass)
    _relay(hass, channels=("l1",))
    body_in = {"rooms": [{"ref": "room_a", "name": "A", "members": [],
                          "actuators": [{"ieee": RELAY, "channel": "l2"},
                                        {"ieee": "0x0000000000000bad", "channel": "l1"}]}]}
    status, body = await _post(hass, body_in)
    assert status == 200
    room = body["rooms"][0]
    assert room["actuators_assigned"] == []
    assert {(u["ieee"], u["reason"]) for u in room["actuators_unknown"]} == {
        (RELAY, "unknown_channel"), ("0x0000000000000bad", "unknown_ieee")}


async def test_a_relay_without_countdown_is_placed_and_reported(hass, config_entry):
    """D7: warn, not block — ga_heating will never switch it ON and says so itself."""
    _seed(hass)
    _relay(hass, countdown=False)
    status, body = await _post(hass, {"rooms": [
        {"ref": "room_a", "name": "A", "members": [],
         "actuators": [{"ieee": RELAY, "channel": "l1"}]}]})
    assert status == 200
    room = body["rooms"][0]
    assert room["actuators_assigned"] == [f"{RELAY}/l1"]
    assert room["actuators_unknown"] == [
        {"ieee": RELAY, "channel": "l1", "reason": "no_countdown"}]
    assert ACTUATOR_LABEL in _sw(hass, "l1").labels


async def test_a_relay_listed_as_member_is_not_placed_as_a_device(hass, config_entry):
    _seed(hass)
    relay = _relay(hass)
    await _post(hass, {"rooms": [
        {"ref": "room_a", "name": "A", "members": [RELAY],
         "actuators": [{"ieee": RELAY, "channel": "l1"}]}]})
    assert dr.async_get(hass).async_get(relay.id).area_id is None


@pytest.mark.parametrize("bad", [
    "not a list",
    [{"ieee": RELAY}],
    [{"ieee": RELAY, "channel": "x1"}],
    [{"ieee": "0xZZ", "channel": "l1"}],
    [{"ieee": RELAY, "channel": "l1", "extra": 1}],
])
async def test_a_malformed_actuator_is_refused(hass, config_entry, bad):
    _seed(hass)
    status, body = await _post(hass, {"rooms": [
        {"ref": "room_a", "name": "A", "members": [], "actuators": bad}]})
    assert status == 400, body
