"""The seed's sensors: battery + linkquality per Zigbee device (see __init__)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

# name, ieee, room (None = no room), model, battery (None = mains), lqi
FLAT = [
    ("Thermostat Küche", "0x00124b00cafe0001", "Küche", "TRVZB", 9, 21),
    ("Thermostat Wohnzimmer", "0x00124b00cafe0002", "Wohnzimmer", "TRVZB", 88, 120),
    ("Steckdose Flur", "0x00124b00cafe0003", None, "S26R2ZB", None, 200),
]
BRIDGE = ("Zigbee2MQTT Bridge", "0x00124b00deadbeef")


class _Sensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: DeviceInfo, ieee: str, key: str, name: str, value) -> None:
        self._attr_device_info = device
        self._attr_unique_id = f"{ieee}_{key}"
        self._attr_name = name
        self._attr_native_value = value


def _battery(device, ieee, value) -> _Sensor:
    s = _Sensor(device, ieee, "battery", "Battery", value)
    s._attr_device_class = SensorDeviceClass.BATTERY
    s._attr_native_unit_of_measurement = PERCENTAGE
    s._attr_entity_category = EntityCategory.DIAGNOSTIC
    return s


def _lqi(device, ieee, value) -> _Sensor:
    s = _Sensor(device, ieee, "linkquality", "Linkquality", value)
    s._attr_native_unit_of_measurement = "lqi"
    s._attr_entity_category = EntityCategory.DIAGNOSTIC
    return s


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    entities = []
    for name, ieee, room, model, battery, lqi in FLAT:
        device = DeviceInfo(
            identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
            name=name,
            model=model,
            manufacturer="e2e",
        )
        if room:
            device["suggested_area"] = room
        if battery is not None:
            entities.append(_battery(device, ieee, battery))
        entities.append(_lqi(device, ieee, lqi))
    bridge_name, bridge_ieee = BRIDGE
    bridge = DeviceInfo(
        identifiers={("mqtt", f"zigbee2mqtt_bridge_{bridge_ieee}")}, name=bridge_name
    )
    entities.append(_lqi(bridge, bridge_ieee, 255))
    async_add_entities(entities)
