"""TEST ONLY — seeds a local Home Assistant Core with a small Zigbee flat.

Used by tests/e2e/local_core/launcher.py so the Einstellungen e2e runs against a
REAL Core with REAL registries. It creates, through the normal entity platform
(not by writing registry files), what zigbee2mqtt would have created:

* Thermostat Küche      — room Küche,       battery 9 %,  LQI 21   (both weak)
* Thermostat Wohnzimmer — room Wohnzimmer,  battery 88 %, LQI 120
* Steckdose Flur        — NO room, mains powered (no battery), LQI 200
* a zigbee2mqtt bridge  — infrastructure, must never be listed

Devices carry the identifiers zigbee2mqtt uses (``("mqtt", "zigbee2mqtt_<ieee>")``)
and linkquality sensors are named like zigbee2mqtt's, so greenautarky_site
recognises them exactly as on a device.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "ga_e2e_seed"
PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"})
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True
