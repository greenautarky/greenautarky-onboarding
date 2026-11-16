"""Read-only facts for the resident's "Einstellungen" page (Odoo #2144, #2145).

Two endpoints, both computed on the server for the same reason the home model is
(see ``rooms._build_home_model``): the browser's own registry lists are not a
reliable source for a non-admin user (a device registry subscription needs
admin; the leak guard filters the rest), and a page that re-derives them there
breaks for exactly the people it is for.

``GET /api/greenautarky_site/zigbee_devices`` (#2144)
    Every Zigbee device with its room (HA area), battery % (only if the device
    has a battery entity) and link quality (LQI, the ``*_linkquality`` sensor
    zigbee2mqtt publishes). Sorted by room, rooms without a name last. A device
    without an area is returned with ``area: null`` — the card says "ohne Raum";
    it is never dropped. The zigbee2mqtt bridge (the coordinator) is not a
    device a resident owns and is left out, as in the home model.

``GET /api/greenautarky_site/versions`` (#2145)
    What this device is running, read where it is observed — never a list kept
    here:

    * the GA release (``BOSv1.6.1-rc2``), OS, Supervisor, Core, plugins and every
      installed add-on come from ga_manager's ``/share/ga_manager/status.json``
      (its ``versions.collect`` health check + ``ga_release``). Only the host can
      read ``/etc/ga-release``; ga_manager is the component that already reports
      it to the fleet, so this is the same number the fleet sees.
    * every custom integration (ga_frontend_bundle, ga_heating, greenautarky_site
      …) comes from Core's own loader — the manifest it actually loaded.

    The file is read on EVERY request: nothing is cached here, so a version that
    changed is shown as it is. ``reported_at`` says how old the ga_manager part
    is, and a missing / unreadable file is reported as such (``status``), never
    papered over with a default.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_custom_components

from ..household.masters import _read_master_user_ids
from ..store import _get_state
from .rooms import SCOPE_ROOMS, _areas, async_scope_for

_LOGGER = logging.getLogger(__name__)

#: ga_manager's health report. Overridable for tests and local runs only; a
#: device always uses the default.
STATUS_FILE_ENV = "GA_MANAGER_STATUS_FILE"
DEFAULT_STATUS_FILE = "/share/ga_manager/status.json"

_RADIO_ADDRESS = re.compile(r"^(?:0x)?[0-9a-f]{16}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Zigbee devices
# ---------------------------------------------------------------------------


def _is_zigbee_device(dev: dr.DeviceEntry) -> bool:
    """A zigbee2mqtt or ZHA end device — not the zigbee2mqtt bridge."""
    for domain, ident in dev.identifiers:
        if domain == "mqtt" and ident.startswith("zigbee2mqtt_bridge_"):
            return False
    for domain, ident in dev.identifiers:
        if domain == "mqtt" and ident.startswith("zigbee2mqtt_"):
            return True
        if domain == "zha":
            return True
    return False


def _number(raw: Any) -> int | None:
    try:
        return round(float(raw))
    except (TypeError, ValueError):
        return None


def _display_name(dev: dr.DeviceEntry) -> str:
    """A resident must never be shown a radio address (see ga-home-strategy)."""
    name = (dev.name_by_user or dev.name or "").strip()
    if name and not _RADIO_ADDRESS.match(name):
        return name
    kind = (dev.model or dev.manufacturer or "Gerät").strip()
    tail = ""
    for _domain, ident in dev.identifiers:
        m = re.search(r"([0-9a-f]{4})$", ident, re.IGNORECASE)
        if m:
            tail = " " + m.group(1)
            break
    return kind + tail


def build_zigbee_devices(
    hass: HomeAssistant, user: Any, scope: str, areas: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """The device rows for one user, sorted by room then name."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    all_areas = {a["area_id"]: a["name"] for a in _areas(hass)}
    allowed = {a["area_id"] for a in areas}
    scoped = scope == SCOPE_ROOMS
    perms = getattr(user, "permissions", None)

    def readable(entity_id: str) -> bool:
        if hass.states.get(entity_id) is None:
            return False
        if scoped and perms is not None:
            return bool(perms.check_entity(entity_id, "read"))
        return True

    by_device: dict[str, list[er.RegistryEntry]] = {}
    for entry in ent_reg.entities.values():
        if entry.device_id and not entry.disabled_by:
            by_device.setdefault(entry.device_id, []).append(entry)

    rows: list[dict[str, Any]] = []
    for dev in dev_reg.devices.values():
        if dev.disabled_by or not _is_zigbee_device(dev):
            continue
        # A room-scoped sub-user sees only devices in the rooms granted to them.
        if scoped and dev.area_id not in allowed:
            continue
        battery = lqi = None
        for ent in by_device.get(dev.id, []):
            if not readable(ent.entity_id) or not ent.entity_id.startswith("sensor."):
                continue
            state = hass.states.get(ent.entity_id)
            if state is None:
                continue
            dclass = state.attributes.get("device_class") or ent.original_device_class
            if dclass == "battery":
                battery = _number(state.state)
            elif ent.entity_id.endswith("_linkquality") or (ent.unique_id or "").find(
                "linkquality"
            ) >= 0:
                lqi = _number(state.state)
        rows.append(
            {
                "device_id": dev.id,
                "name": _display_name(dev),
                "area_id": dev.area_id,
                # None, not "": the card owns the wording for a device with no room.
                "area": all_areas.get(dev.area_id) if dev.area_id else None,
                "battery": battery,
                "lqi": lqi,
            }
        )

    # Rooms alphabetically, devices without a room last, then by device name.
    rows.sort(
        key=lambda r: (
            r["area"] is None,
            (r["area"] or "").casefold(),
            r["name"].casefold(),
        )
    )
    return rows


class GAZigbeeDevicesView(HomeAssistantView):
    """Zigbee devices with room, battery and LQI — read-only, scoped like the home model."""

    url = "/api/greenautarky_site/zigbee_devices"
    name = "api:greenautarky_site:zigbee_devices"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        state = _get_state(hass)
        masters = await hass.async_add_executor_job(_read_master_user_ids, hass)
        sub_users = state.get("sub_users") or {}
        scope, _reason, areas = await async_scope_for(hass, user, state, masters, sub_users)
        return self.json({"devices": build_zigbee_devices(hass, user, scope, areas)})


# ---------------------------------------------------------------------------
# Software versions
# ---------------------------------------------------------------------------

_PLUGIN_LABELS = {
    "cli": "Supervisor-Plugin CLI",
    "dns": "Supervisor-Plugin DNS",
    "audio": "Supervisor-Plugin Audio",
    "multicast": "Supervisor-Plugin Multicast",
    "observer": "Supervisor-Plugin Observer",
}


def _status_path() -> Path:
    return Path(os.environ.get(STATUS_FILE_ENV) or DEFAULT_STATUS_FILE)


def _read_status(path: Path) -> tuple[str, dict[str, Any] | None]:
    """``(status, report)`` — status is ``ok`` / ``missing`` / ``unreadable``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except OSError as err:
        _LOGGER.warning("cannot read %s: %s", path, err)
        return "unreadable", None
    try:
        report = json.loads(raw)
    except ValueError as err:
        _LOGGER.warning("%s is not JSON: %s", path, err)
        return "unreadable", None
    return ("ok", report) if isinstance(report, dict) else ("unreadable", None)


def _snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """The ``versions.collect`` details out of a ga_manager health report."""
    for check in report.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == "versions.collect":
            details = check.get("details")
            return details if isinstance(details, dict) else {}
    return {}


def _ver(node: Any) -> str | None:
    v = node.get("version") if isinstance(node, dict) else None
    return str(v) if v else None


def components_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """System layers and add-ons as ga_manager observed them."""
    snap = _snapshot(report)
    out: list[dict[str, Any]] = []

    def add(group: str, key: str, name: str, version: str | None) -> None:
        if version:
            out.append({"group": group, "id": key, "name": name, "version": version})

    add("system", "os", "Betriebssystem (OS)", _ver(snap.get("os")))
    add("system", "supervisor", "Supervisor", _ver(snap.get("supervisor")))
    add("system", "core", "Home Assistant Core", _ver(snap.get("core")))
    front = snap.get("frontend")
    if isinstance(front, dict):
        add("system", "frontend", "Home Assistant Frontend",
            str(front.get("version") or front.get("pyversion") or "") or None)
    for plugin, node in sorted((snap.get("plugins") or {}).items()):
        add("system", f"plugin_{plugin}", _PLUGIN_LABELS.get(plugin, f"Plugin {plugin}"),
            _ver(node))
    addons = snap.get("addons") or {}
    for slug in sorted(addons, key=lambda s: str((addons[s] or {}).get("name") or s).casefold()):
        node = addons[slug] or {}
        add("addon", slug, str(node.get("name") or slug), _ver(node))
    return out


async def components_from_core(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every custom integration Core loaded, with the version in its manifest."""
    out: list[dict[str, Any]] = []
    for domain, integration in sorted((await async_get_custom_components(hass)).items()):
        version = getattr(integration, "version", None)
        if version:
            out.append({"group": "integration", "id": domain,
                        "name": integration.name or domain, "version": str(version)})
    return out


async def build_versions(hass: HomeAssistant) -> dict[str, Any]:
    status, report = await hass.async_add_executor_job(
        lambda: _read_status(_status_path()))
    components: list[dict[str, Any]] = []
    bundle = None
    reported_at = None
    if report is not None:
        rel = report.get("ga_release")
        bundle = str(rel).strip() if isinstance(rel, str) and rel.strip() else None
        reported_at = report.get("timestamp")
        components.extend(components_from_report(report))
    components.extend(await components_from_core(hass))
    return {
        "status": status,
        "bundle": bundle,
        "reported_at": reported_at,
        "components": components,
    }


class GAVersionsView(HomeAssistantView):
    """What this device runs, as observed — read fresh on every request."""

    url = "/api/greenautarky_site/versions"
    name = "api:greenautarky_site:versions"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        return self.json(await build_versions(hass))
