"""A room offers an explicitly chosen set of controls — never "everything else".

THE DEFECT THIS FILE EXISTS FOR, measured on a bench flat on 2026-09-16.

A room view offered residents a switch labelled "Smart temperature control" —
English, unexplained, one per thermostat. It is not ours: a Sonoff TRV publishes
`smart_temperature_control`, zigbee2mqtt forwards it, Home Assistant makes a
switch, and the room listed it because nothing said not to. It carries
`entity_category: None` — the vendor calls it a primary control — so every
filter keyed on that category let it through. It is not cosmetic either: it
changes how the valve regulates.

Its two siblings on the same device tell the whole story: `child_lock` and
`open_window` carry `config` and were filtered. Whether a resident saw a vendor
knob depended on how the vendor had labelled it.

A deny-list cannot win that race — the next firmware adds an entity and it
appears on a resident's screen with nobody deciding that it should. So the
device's ROLE decides and each role names what it offers (ADR-0014 Amendment 1).

The test that matters is the one with an UNKNOWN entity: asserting that the
known controls still appear cannot catch the next firmware.
"""
from __future__ import annotations

import json

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_rooms import _area, _User

from greenautarky_site.scoping import rooms
from greenautarky_site.scoping.rooms import SCOPE_ALL


@pytest.fixture
def config_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="allow-list-entry")
    entry.add_to_hass(hass)
    return entry


def _device(hass, config_entry_id, name, ident):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry_id,
        identifiers={("mqtt", ident)},
        name=name,
    )


def _entity(hass, domain, object_id, *, area=None, device=None, state="on",
            attrs=None, category=None):
    reg = er.async_get(hass)
    reg.async_get_or_create(
        domain, "mqtt", f"{object_id}-uid", suggested_object_id=object_id,
        device_id=device.id if device else None, entity_category=category,
    )
    eid = f"{domain}.{object_id}"
    if area is not None and device is None:
        reg.async_update_entity(eid, area_id=area.id)
    hass.states.async_set(eid, state, attrs or {})
    return eid


async def _model(hass, area):
    return rooms._build_home_model(
        hass, _User("u1"), SCOPE_ALL, [{"area_id": area.id, "name": area.name}])


async def _trv_room(hass, config_entry):
    """A room as a real flat has it: one valve exposing a thermostat and the
    three switches the vendor ships with it."""
    area = await _area(hass, "Wohnzimmer")
    valve = _device(hass, config_entry.entry_id, "0x0cae5ffffeac0a2c", "z2m_trv")
    dr.async_get(hass).async_update_device(valve.id, area_id=area.id)
    _entity(hass, "climate", "trv", device=valve, state="heat")
    _entity(hass, "switch", "trv_child_lock", device=valve, category=EntityCategory.CONFIG)
    _entity(hass, "switch", "trv_open_window", device=valve, category=EntityCategory.CONFIG)
    _entity(hass, "switch", "trv_smart_temperature_control", device=valve)
    return area, valve


async def test_a_vendor_knob_on_a_thermostat_is_not_offered(hass, config_entry):
    """THE RED ONE. `entity_category: None` and therefore past every
    category filter — and still not a household control."""
    area, _ = await _trv_room(hass, config_entry)

    model = await _model(hass, area)

    assert "smart_temperature_control" not in json.dumps(model)


async def test_the_thermostat_itself_is_still_offered(hass, config_entry):
    """Must-not-flag. A rule that removed the vendor knob and the thermostat
    with it would leave the room unusable, which is worse than the defect."""
    area, _ = await _trv_room(hass, config_entry)

    model = await _model(hass, area)

    assert model["rooms"][0]["climate"] == ["climate.trv"]


async def test_an_entity_the_next_firmware_adds_is_absent_by_default(hass, config_entry):
    """THE ONE THIS DESIGN EXISTS FOR. Nobody has heard of this entity; it must
    not reach a resident's screen because nobody has excluded it yet.

    Asserting only that the KNOWN controls appear would be green on the day
    this happens, which is how the defect arrived in the first place.
    """
    area, valve = await _trv_room(hass, config_entry)
    _entity(hass, "switch", "trv_some_future_vendor_mode", device=valve)

    model = await _model(hass, area)

    assert "some_future_vendor_mode" not in json.dumps(model)


async def test_a_real_switch_is_still_a_control(hass, config_entry):
    """A plug is a switch and must stay operable — the allow-list is per ROLE,
    so a device that is a switch offers its switch."""
    area = await _area(hass, "Wohnzimmer")
    plug = _device(hass, config_entry.entry_id, "Steckdose", "z2m_plug")
    dr.async_get(hass).async_update_device(plug.id, area_id=area.id)
    _entity(hass, "switch", "stehlampe", device=plug)

    model = await _model(hass, area)

    assert model["rooms"][0]["switches"] == ["switch.stehlampe"]


async def test_a_light_is_still_a_control(hass, config_entry):
    area = await _area(hass, "Wohnzimmer")
    lamp = _device(hass, config_entry.entry_id, "Lampe", "z2m_lamp")
    dr.async_get(hass).async_update_device(lamp.id, area_id=area.id)
    _entity(hass, "light", "decke", device=lamp)

    model = await _model(hass, area)

    assert model["rooms"][0]["lights"] == ["light.decke"]


async def test_an_entity_without_a_device_is_kept(hass, config_entry):
    """A device is what firmware brings; a deviceless entity exists because a
    PERSON configured it — a template light, a helper. Refusing those would
    take away what somebody built on purpose in order to keep out what nobody
    chose."""
    area = await _area(hass, "Wohnzimmer")
    _entity(hass, "light", "template_licht", area=area)

    model = await _model(hass, area)

    assert model["rooms"][0]["lights"] == ["light.template_licht"]


async def test_the_sensors_a_thermostat_provides_still_reach_the_charts(hass, config_entry):
    """The allow-list governs CONTROLS. A valve's temperature reading is not a
    control and must keep feeding the history charts, or the room loses its
    curves along with the knob."""
    area, valve = await _trv_room(hass, config_entry)
    _entity(hass, "sensor", "trv_local_temperature", device=valve,
            state="21.5", attrs={"device_class": "temperature"})

    model = await _model(hass, area)

    assert model["rooms"][0]["temps"] == ["sensor.trv_local_temperature"]

