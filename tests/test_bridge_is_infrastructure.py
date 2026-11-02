"""The Zigbee coordinator is infrastructure, not a household device.

THE DEFECT THIS FILE EXISTS FOR, seen on a bench device on 2026-11-02 (#1479).

A freshly onboarded master landed on the flat "no rooms yet" overview and found a
switch "Zigbee2MQTT Bridge Permit join" on it — one tap opens the Zigbee network
for pairing for 254 s. Nobody chose to offer it:

- zigbee2mqtt has always published the bridge's ``permit_join`` as a primary
  switch (no ``entity_category``, checked in 1.40.2 through 2.12.1);
- the allow-list reads a device's ROLE from what it exposes, and a device that
  exposes a ``switch.*`` is a plug — so the coordinator passed as a plug;
- the bridge has no area, so it lands in ``roomless``, which the overview renders
  flat until devices are placed in rooms. On an installed flat the roomless view
  is hidden, which is why it was never seen there.

The bridge keeps all its entities in Home Assistant — the installer pairs devices
with exactly this switch. It is only never OFFERED to a resident.
"""
from __future__ import annotations

import json

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from test_resident_controls_allow_list import _device, _entity
from test_rooms import _area, _User

from greenautarky_site.scoping import rooms
from greenautarky_site.scoping.rooms import SCOPE_ALL

# The identifier zigbee2mqtt gives its bridge device: `zigbee2mqtt_bridge_` + the
# coordinator's IEEE address (lib/extension/homeassistant.ts, getDevicePayload).
BRIDGE_IDENT = "zigbee2mqtt_bridge_0x00124b00deadbeef"


@pytest.fixture
def config_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="bridge-entry")
    entry.add_to_hass(hass)
    return entry


def _flat(hass, config_entry, area=None):
    """The coordinator as zigbee2mqtt registers it, next to a plug and a valve."""
    bridge = _device(hass, config_entry.entry_id, "Zigbee2MQTT Bridge", BRIDGE_IDENT)
    _entity(hass, "switch", "zigbee2mqtt_bridge_permit_join", device=bridge, state="off")
    _entity(hass, "button", "zigbee2mqtt_bridge_restart", device=bridge, state="unknown")
    _entity(hass, "select", "zigbee2mqtt_bridge_log_level", device=bridge,
            state="info", category=EntityCategory.CONFIG)
    _entity(hass, "binary_sensor", "zigbee2mqtt_bridge_connection_state", device=bridge,
            category=EntityCategory.DIAGNOSTIC)

    plug = _device(hass, config_entry.entry_id, "Steckdose", "zigbee2mqtt_0x00124b00cafe0002")
    _entity(hass, "switch", "stehlampe", device=plug)
    valve = _device(hass, config_entry.entry_id, "0x00124b00cafe0001", "zigbee2mqtt_0x00124b00cafe0001")
    _entity(hass, "climate", "trv", device=valve, state="heat")

    if area is not None:
        for dev in (bridge, plug, valve):
            dr.async_get(hass).async_update_device(dev.id, area_id=area.id)


async def _model(hass, area):
    return rooms._build_home_model(
        hass, _User("u1"), SCOPE_ALL, [{"area_id": area.id, "name": area.name}])


async def test_the_bridge_is_not_offered_on_the_no_rooms_overview(hass, config_entry):
    """THE RED ONE — the device as it was on 2026-11-02: rooms exist, no device
    is placed in one, everything is roomless."""
    area = await _area(hass, "Wohnzimmer")
    _flat(hass, config_entry)

    model = await _model(hass, area)

    assert "zigbee2mqtt_bridge" not in json.dumps(model), model


async def test_plug_and_thermostat_stay_on_the_no_rooms_overview(hass, config_entry):
    """Must-pass. A rule that took the bridge away together with every switch
    or thermostat would leave a new flat with nothing to operate."""
    area = await _area(hass, "Wohnzimmer")
    _flat(hass, config_entry)

    model = await _model(hass, area)

    # Membership, not equality: this one must be green with AND without the fix,
    # so it says nothing about the bridge — only that the real controls survive.
    assert "switch.stehlampe" in model["roomless"]["switches"]
    assert "climate.trv" in model["roomless"]["climate"]


async def test_the_bridge_is_not_offered_in_a_room_either(hass, config_entry):
    """An installer may well put the coordinator into a room. It is still not a
    household control there."""
    area = await _area(hass, "Wohnzimmer")
    _flat(hass, config_entry, area=area)

    model = await _model(hass, area)

    assert "zigbee2mqtt_bridge" not in json.dumps(model), model
    assert model["rooms"][0]["switches"] == ["switch.stehlampe"]
    assert model["rooms"][0]["climate"] == ["climate.trv"]
