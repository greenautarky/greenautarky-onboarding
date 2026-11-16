"""The resident's "Einstellungen" page facts (Odoo #2144 Zigbee list, #2145 versions).

Two things are pinned here, both measured on the K31 hand test of
BOSv1.6.1-rc1 (2026-11-16, Thomas):

* the Zigbee list must hold EVERY Zigbee device — one without a room is shown
  (area ``None``), never dropped — and must carry battery only where the device
  has one;
* the version tile must show what the device runs NOW. The ga_manager file is
  read on every request; the second request after the file changed must return
  the new value (a stale cache is the defect the ticket's red proof is about).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_rooms import _area, _User

from greenautarky_site.scoping import resident_info
from greenautarky_site.scoping.rooms import SCOPE_ALL, SCOPE_ROOMS

# Pinned expectations — never derived from the code under test.
IEEE = "0x00124b00cafe0001"


@pytest.fixture
def config_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt", entry_id="zb-entry")
    entry.add_to_hass(hass)
    return entry


def _zdev(hass, entry, name, ident, area=None, model="TRVZB"):
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mqtt", ident)},
        name=name,
        model=model,
    )
    if area is not None:
        dr.async_get(hass).async_update_device(dev.id, area_id=area.id)
    return dev


def _sensor(hass, dev, object_id, state, device_class=None):
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor", "mqtt", f"{object_id}-uid", suggested_object_id=object_id,
        device_id=dev.id,
    )
    attrs = {"device_class": device_class} if device_class else {}
    hass.states.async_set(f"sensor.{object_id}", state, attrs)


async def _flat(hass, entry):
    kitchen = await _area(hass, "Küche")
    living = await _area(hass, "Wohnzimmer")
    # Deliberately created out of room order.
    a = _zdev(hass, entry, "Thermostat Wohnzimmer", "zigbee2mqtt_0x01", area=living)
    _sensor(hass, a, "thermostat_wohnzimmer_battery", "88", "battery")
    _sensor(hass, a, "thermostat_wohnzimmer_linkquality", "120")
    b = _zdev(hass, entry, "Thermostat Küche", "zigbee2mqtt_0x02", area=kitchen)
    _sensor(hass, b, "thermostat_kueche_battery", "9", "battery")
    _sensor(hass, b, "thermostat_kueche_linkquality", "21")
    # Mains powered: no battery entity at all.
    c = _zdev(hass, entry, "Steckdose Flur", "zigbee2mqtt_0x03", area=None, model="S26R2ZB")
    _sensor(hass, c, "steckdose_flur_linkquality", "200")
    # The coordinator is infrastructure.
    _zdev(hass, entry, "Zigbee2MQTT Bridge", "zigbee2mqtt_bridge_0x00124b00deadbeef")
    # Not Zigbee at all.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hue", "bridge1")}, name="Hue Bridge")
    return kitchen, living


async def _rows(hass, scope=SCOPE_ALL, areas=None):
    return resident_info.build_zigbee_devices(hass, _User("u1"), scope, areas or [])


async def test_every_zigbee_device_is_listed_sorted_by_room(hass, config_entry):
    await _flat(hass, config_entry)

    rows = await _rows(hass)

    assert [(r["name"], r["area"]) for r in rows] == [
        ("Thermostat Küche", "Küche"),
        ("Thermostat Wohnzimmer", "Wohnzimmer"),
        ("Steckdose Flur", None),  # no room: last, and present
    ]


async def test_battery_and_lqi_come_from_the_device_entities(hass, config_entry):
    await _flat(hass, config_entry)

    rows = {r["name"]: r for r in await _rows(hass)}

    assert (rows["Thermostat Küche"]["battery"], rows["Thermostat Küche"]["lqi"]) == (9, 21)
    assert (rows["Thermostat Wohnzimmer"]["battery"], rows["Thermostat Wohnzimmer"]["lqi"]) == (88, 120)
    # Mains powered: battery is None (not 0), LQI still there.
    assert (rows["Steckdose Flur"]["battery"], rows["Steckdose Flur"]["lqi"]) == (None, 200)


async def test_a_device_without_a_room_is_returned_not_dropped(hass, config_entry):
    """THE RED ONE of #2144: 'ohne Raum', not missing."""
    await _flat(hass, config_entry)

    rows = await _rows(hass)

    roomless = [r for r in rows if r["area_id"] is None]
    assert [r["name"] for r in roomless] == ["Steckdose Flur"]
    assert roomless[0]["area"] is None


async def test_bridge_and_foreign_devices_are_not_listed(hass, config_entry):
    await _flat(hass, config_entry)

    names = [r["name"] for r in await _rows(hass)]

    assert "Zigbee2MQTT Bridge" not in names
    assert "Hue Bridge" not in names


async def test_a_radio_address_is_never_shown_as_a_name(hass, config_entry):
    _zdev(hass, config_entry, IEEE, "zigbee2mqtt_" + IEEE, model="TRVZB")

    [row] = await _rows(hass)

    assert IEEE not in row["name"]
    assert row["name"] == "TRVZB 0001"


async def test_an_unavailable_reading_is_none(hass, config_entry):
    a = _zdev(hass, config_entry, "Sensor", "zigbee2mqtt_0x09")
    _sensor(hass, a, "sensor_battery", "unavailable", "battery")
    _sensor(hass, a, "sensor_linkquality", "unknown")

    [row] = await _rows(hass)

    assert (row["battery"], row["lqi"]) == (None, None)


async def test_a_room_scoped_user_sees_only_their_rooms(hass, config_entry):
    kitchen, _living = await _flat(hass, config_entry)

    rows = await _rows(hass, SCOPE_ROOMS, [{"area_id": kitchen.id, "name": "Küche"}])

    assert [r["name"] for r in rows] == ["Thermostat Küche"]


# ─── versions ────────────────────────────────────────────────────────────


def _report(release="BOSv1.6.1-rc2", core="2026.10.4", addon="0.277.5") -> dict[str, Any]:
    return {
        "timestamp": "2026-11-16T10:00:00Z",
        "ga_release": release,
        "checks": [
            {"name": "uptime", "state": "ok"},
            {
                "name": "versions.collect",
                "state": "ok",
                "details": {
                    "os": {"version": "17.1.ga1"},
                    "supervisor": {"version": "2026.10.2"},
                    "core": {"version": core},
                    "frontend": {"pyversion": "20261028.5"},
                    "plugins": {"dns": {"version": "2026.10.0"}},
                    "addons": {
                        "99f1cad4_ga_manager": {"name": "GA Manager", "version": addon},
                        "45df7312_zigbee2mqtt": {"name": "Zigbee2MQTT", "version": "2.12.1"},
                    },
                },
            },
        ],
    }


@pytest.fixture
def status_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "status.json"
    monkeypatch.setenv(resident_info.STATUS_FILE_ENV, str(path))
    return path


async def test_versions_report_the_bundle_and_every_layer(hass, status_file):
    status_file.write_text(json.dumps(_report()))

    out = await resident_info.build_versions(hass)

    assert out["status"] == "ok"
    assert out["bundle"] == "BOSv1.6.1-rc2"
    got = {c["id"]: c["version"] for c in out["components"]}
    assert got["os"] == "17.1.ga1"
    assert got["supervisor"] == "2026.10.2"
    assert got["core"] == "2026.10.4"
    assert got["99f1cad4_ga_manager"] == "0.277.5"
    assert got["45df7312_zigbee2mqtt"] == "2.12.1"
    assert got["plugin_dns"] == "2026.10.0"
    assert got["frontend"] == "20261028.5"


async def test_a_changed_version_is_shown_as_it_is_no_stale_value(hass, status_file):
    """THE RED ONE of #2145 ('no caching of a stale value')."""
    status_file.write_text(json.dumps(_report(release="BOSv1.6.1-rc1", core="2026.10.3")))
    first = await resident_info.build_versions(hass)
    status_file.write_text(json.dumps(_report(release="BOSv1.6.1-rc2", core="2026.10.4")))

    second = await resident_info.build_versions(hass)

    assert first["bundle"] == "BOSv1.6.1-rc1"
    assert second["bundle"] == "BOSv1.6.1-rc2"
    assert {c["id"]: c["version"] for c in second["components"]}["core"] == "2026.10.4"


async def test_a_missing_report_is_said_not_defaulted(hass, status_file):
    out = await resident_info.build_versions(hass)

    assert out["status"] == "missing"
    assert out["bundle"] is None
    assert [c for c in out["components"] if c["group"] in ("system", "addon")] == []


async def test_a_corrupt_report_is_unreadable_not_empty_ok(hass, status_file):
    status_file.write_text("{not json")

    out = await resident_info.build_versions(hass)

    assert out["status"] == "unreadable"
    assert out["bundle"] is None


async def test_a_report_without_a_release_has_no_bundle(hass, status_file):
    rep = _report()
    del rep["ga_release"]
    status_file.write_text(json.dumps(rep))

    out = await resident_info.build_versions(hass)

    assert out["status"] == "ok"
    assert out["bundle"] is None


async def test_core_integrations_come_from_the_loader(hass, status_file, monkeypatch):
    """The integration versions are the manifests Core loaded — not a list here."""

    class _Integ:
        def __init__(self, name, version):
            self.name, self.version = name, version

    async def fake(_hass):
        return {"ga_heating": _Integ("GA Heating", "0.16.1"),
                "ga_frontend_bundle": _Integ("GA Frontend Bundle", "1.23.11")}

    monkeypatch.setattr(resident_info, "async_get_custom_components", fake)

    out = await resident_info.build_versions(hass)

    ints = {c["id"]: c["version"] for c in out["components"] if c["group"] == "integration"}
    assert ints == {"ga_heating": "0.16.1", "ga_frontend_bundle": "1.23.11"}
