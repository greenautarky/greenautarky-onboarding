"""The master-user flag — the server-side authorization boundary.

A "Master-User" is a HA Non-Admin flagged in
``/config/ga/ga-master-users.json``, written by ga_manager /
ga-fleet-manager (this component normally only READS it; the write path
here backs the prototype ``set_master`` op + onboarding auto-election).
Fail CLOSED: a missing or malformed flag file yields no masters, so a
broken flag can never grant privilege.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from ..const import MASTER_USERS_FILE

_LOGGER = logging.getLogger(__name__)

def _master_users_path(hass: HomeAssistant) -> Path:
    """Path to the master-user allowlist (read-only here; ga_manager writes)."""
    return Path(hass.config.path(MASTER_USERS_FILE))


def _read_master_user_ids(hass: HomeAssistant) -> set[str]:
    """Return the set of HA user-ids flagged as masters.

    Fail CLOSED: a missing or malformed file yields an empty set (no masters),
    so a broken/absent flag can never grant privilege. Format:
    ``{"masters": [{"ha_user_id": "<uuid>"}, ...]}``.
    """
    path = _master_users_path(hass)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.warning("master-users: %s is not valid JSON — treating as empty", path)
        return set()
    ids: set[str] = set()
    for entry in data.get("masters") or []:
        uid = (entry or {}).get("ha_user_id") if isinstance(entry, dict) else None
        if isinstance(uid, str) and uid:
            ids.add(uid)
    return ids


def _is_master(hass: HomeAssistant, user_id: str | None) -> bool:
    """True iff user_id is a flagged master (SYNC — reads a file; use only off
    the event loop, e.g. in tests / executor). Async handlers must use
    ``_async_is_master`` so the flag read doesn't block the loop."""
    return bool(user_id) and user_id in _read_master_user_ids(hass)


async def _async_is_master(hass: HomeAssistant, user_id: str | None) -> bool:
    """Master check with the flag-file read off-loop (executor)."""
    if not user_id:
        return False
    ids = await hass.async_add_executor_job(_read_master_user_ids, hass)
    return user_id in ids


def _write_master_users(hass: HomeAssistant, ids: set[str]) -> None:
    """Persist the master allowlist to /config/ga/ga-master-users.json."""
    path = _master_users_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"masters": [{"ha_user_id": i} for i in sorted(ids)]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def _require_master(request: web.Request) -> tuple[Any, web.Response | None]:
    """Return (user, None) if the authenticated caller is a master, else
    (None, 403-response). Flag read is off-loop."""
    hass: HomeAssistant = request.app["hass"]
    user = request["hass_user"]
    if not await _async_is_master(hass, getattr(user, "id", None)):
        return None, web.json_response(
            {"message": "Master privileges required"}, status=403
        )
    return user, None
