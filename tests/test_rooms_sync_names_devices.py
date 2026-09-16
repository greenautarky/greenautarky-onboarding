"""A device placed in a room gets a name a resident can read.

THE DEFECT THIS FILE EXISTS FOR, measured on a bench device on 2026-09-16.

Every paired device kept the name zigbee2mqtt gave it — its IEEE address — so
the resident's screen showed `0x0cae5ffffeac0a2c` wherever a name belonged: in
chart legends, on device tiles, in more-info. Worse, the 24-hour humidity chart
drew three series from ONE sensor (min/mean/max) and labelled all three with
the same address, so the legend carried no information at all.

Placement is the one moment where the name can be derived: the room is known
and the device's entities exist. Before it there is nothing to derive from;
afterwards nobody looks again.

The kind comes from what the device CAN DO, never from a model table — a table
is wrong on the day a different valve is sourced, and wrong silently, because
the name just stays an address again.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_rooms_sync import _body, _post, _seed


@pytest.fixture
def config_entry(hass):
    """A local copy rather than an import: importing a fixture makes it a
    module-level name that every test parameter then shadows (ruff F811), and
    a `noqa` on seven lines is a worse trade than four lines here."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="test-entry")
    entry.add_to_hass(hass)
    return entry

TRV = "0x0cae5ffffeac0a2c"
TRV2 = "0x0cae5ffffeb44478"
DISPLAY = "0x449fdafffe7df6bb"


def _z2m_device(hass, ieee: str):
    """A device registry entry as zigbee2mqtt REALLY leaves it.

    The shared helper in test_rooms_sync names devices "sensor <ieee>", which no
    device on a real flat is called. Measured on a bench device 2026-09-16: the
    name is the bare address and nothing else, which is the whole reason this
    file exists — so the fixture says the address and nothing else too.
    """
    return dr.async_get(hass).async_get_or_create(
        config_entry_id="test-entry",
        identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
        name=ieee,
    )


def _entity(hass, device_id, domain, object_id, device_class=None):
    er.async_get(hass).async_get_or_create(
        domain, "mqtt", f"{object_id}-uid", device_id=device_id,
        suggested_object_id=object_id, original_device_class=device_class,
    )


def _room(members, name="Wohnzimmer", ref="room_wohnzimmer"):
    return {"mode": "merge", "rooms": [
        {"name": name, "ref": ref, "members": members}]}


@pytest.mark.asyncio
async def test_a_thermostat_is_named_thermostat(hass, config_entry):
    _seed(hass)
    dev = _z2m_device(hass, TRV)
    _entity(hass, dev.id, "climate", "trv")

    resp = await _post(hass, _room([TRV]))

    assert resp.status == 200, _body(resp)
    assert dr.async_get(hass).async_get(dev.id).name_by_user == "Thermostat 1"


@pytest.mark.asyncio
async def test_two_thermostats_in_one_room_are_numbered(hass, config_entry):
    """THE ONE THAT MATTERS FOR THE LEGEND. Two valves that both read
    "Thermostat" would leave the chart exactly as unreadable as the addresses
    did."""
    _seed(hass)
    a = _z2m_device(hass, TRV)
    b = _z2m_device(hass, TRV2)
    _entity(hass, a.id, "climate", "trv_a")
    _entity(hass, b.id, "climate", "trv_b")

    await _post(hass, _room([TRV, TRV2]))

    reg = dr.async_get(hass)
    names = sorted(filter(None, (reg.async_get(a.id).name_by_user,
                                 reg.async_get(b.id).name_by_user)))
    assert names == ["Thermostat 1", "Thermostat 2"], names


@pytest.mark.asyncio
async def test_a_temperature_and_humidity_sensor_is_a_klimasensor(hass, config_entry):
    _seed(hass)
    dev = _z2m_device(hass, DISPLAY)
    _entity(hass, dev.id, "sensor", "t", device_class="temperature")
    _entity(hass, dev.id, "sensor", "h", device_class="humidity")

    await _post(hass, _room([DISPLAY]))

    assert dr.async_get(hass).async_get(dev.id).name_by_user == "Klimasensor 1"


@pytest.mark.asyncio
async def test_a_name_a_human_chose_is_never_touched(hass, config_entry):
    """THE RULE. A resident who renamed their valve must find that name again,
    and `name_by_user` set at all means a human decided — including when what
    they typed happens to look like an address."""
    _seed(hass)
    dev = _z2m_device(hass, TRV)
    _entity(hass, dev.id, "climate", "trv")
    dr.async_get(hass).async_update_device(dev.id, name_by_user="Heizung Fenster")

    await _post(hass, _room([TRV]))

    assert dr.async_get(hass).async_get(dev.id).name_by_user == "Heizung Fenster"


@pytest.mark.asyncio
async def test_a_device_that_already_has_a_real_name_is_left_alone(hass, config_entry):
    """Only a raw radio address is replaced. A device an integration named
    sensibly keeps that name."""
    _seed(hass)
    reg = dr.async_get(hass)
    dev = reg.async_get_or_create(
        config_entry_id="test-entry",
        identifiers={("mqtt", f"zigbee2mqtt_{TRV}")},
        name="Danfoss Ally",
    )
    _entity(hass, dev.id, "climate", "trv")

    await _post(hass, _room([TRV]))

    after = reg.async_get(dev.id)
    assert after.name_by_user is None
    assert after.name == "Danfoss Ally"


@pytest.mark.asyncio
async def test_an_unknown_device_kind_is_left_unnamed_rather_than_guessed(hass, config_entry):
    """A switch is not a thermostat and not a sensor. Inventing a name for it
    would be worse than the address: at least an address is honest."""
    _seed(hass)
    dev = _z2m_device(hass, TRV)
    _entity(hass, dev.id, "switch", "relay")

    await _post(hass, _room([TRV]))

    assert dr.async_get(hass).async_get(dev.id).name_by_user is None


@pytest.mark.asyncio
async def test_naming_is_not_repeated_on_a_second_sync(hass, config_entry):
    """Syncs repeat. A second one must not walk the number up."""
    _seed(hass)
    dev = _z2m_device(hass, TRV)
    _entity(hass, dev.id, "climate", "trv")

    await _post(hass, _room([TRV]))
    await _post(hass, _room([TRV]))

    assert dr.async_get(hass).async_get(dev.id).name_by_user == "Thermostat 1"
