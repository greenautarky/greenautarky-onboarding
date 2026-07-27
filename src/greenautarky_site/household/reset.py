"""Resident self-service reset — the two Danger-Zone actions (KB #169).

The household can already be *managed* from the "Verwalten" tab; until now it
could not be *unmade* from there. Deleting a tenant was an operator-only act in
the fleet-manager, which is the wrong shape for a GDPR erasure request from the
person whose data it is.

Two actions, deliberately different in blast radius:

  ``household/reset``    Remove every sub-user of the calling master. Pure
                         Core-side, no restart. The home, its rooms, devices,
                         automations and the master's own account survive.
                         NB: recorder history is entity-bound, not user-bound,
                         so a departed sub-user's measurements are NOT removed
                         here — only the site reset can do that, and the UI
                         copy has to say so.

  ``site_reset/request`` The full tenant wipe. Core cannot stop Core, so the
                         work belongs to the ga_manager addon; this only files
                         a request and reports back what the addon did with it.

Why a marker file and not an HTTP call: ga_manager's bearer token is
all-or-nothing today (OTA, docker exec, everything), so handing it to Core
would trade a ``/config`` read for full device control. The marker grants
exactly one capability — "ask for a wipe" — and leaves the addon as the
enforcement point: it re-validates the request, owns the WIPE/KEEP manifest,
and is free to refuse.

Gating on the site reset is three-deep: the master flag (server-side, the card
being rendered is never the security half), a FRESH sticker-PIN check (proof of
physical presence, backed by its own backoff counters), and a typed
confirmation phrase.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from ..const import (
    RESET_CONFIRM_PHRASE,
    RESET_PIN_SCOPE,
    RESET_REQUEST_FILE,
    RESET_REQUEST_SCHEMA_VERSION,
    RESET_REQUEST_TTL_S,
    RESET_STATUS_FILE,
)
from ..onboarding.pin import async_verify_pin_fresh
from ..store import _get_state, _get_store
from .dashboards_admin import _reconcile_dashboard_visibility
from .masters import _require_master
from .sub_users import (
    _AdminRemovalRefused,
    _async_remove_sub_user,
    _children_of,
)

_LOGGER = logging.getLogger(__name__)


def _confirm_ok(body: dict[str, Any]) -> bool:
    """True iff the caller typed the confirmation phrase."""
    return (body.get("confirm") or "").strip().upper() == RESET_CONFIRM_PHRASE


def _request_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(RESET_REQUEST_FILE))


def _status_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(RESET_STATUS_FILE))


def _write_request(path: Path, payload: dict[str, Any]) -> None:
    """Write the marker atomically (SYNC — call via the executor).

    Atomic because the addon polls this path: a reader must never catch a
    half-written JSON document and decide it is malformed (we would drop a
    real wipe request) — or worse, parse a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, or None if absent/unreadable/garbage (SYNC)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class GAHouseholdResetView(HomeAssistantView):
    """Master-only: remove every sub-user of the calling master.

    The soft half of the Danger Zone. Idempotent — running it on an empty
    household is a no-op that still reports ok.
    """

    url = "/api/greenautarky_site/household/reset"
    name = "api:greenautarky_site:household_reset"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Delete all sub-users owned by the calling master."""
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        if not _confirm_ok(body):
            return web.json_response(
                {"message": f"Confirmation phrase required ({RESET_CONFIRM_PHRASE})"},
                status=400,
            )

        state = _get_state(hass)
        store = _get_store(hass)

        removed: list[str] = []
        skipped: list[str] = []
        to_reconcile: list[str] = []
        # Snapshot the ids first — _async_remove_sub_user mutates the very dict
        # _children_of reads from.
        for sub_user_id in list(_children_of(state, master.id)):
            try:
                to_reconcile += await _async_remove_sub_user(hass, state, sub_user_id)
            except _AdminRemovalRefused:
                # Should not happen (an admin is not a sub-user), but a
                # mislabelled entry must not abort the whole reset.
                _LOGGER.warning(
                    "household reset: refusing to remove admin-flagged %s",
                    sub_user_id,
                )
                skipped.append(sub_user_id)
                continue
            removed.append(sub_user_id)

        # Invites this master issued are dead too — otherwise a PIN handed out
        # yesterday would re-create a household the owner just cleared.
        invites = state.get("sub_user_invites") or []
        state["sub_user_invites"] = [
            i for i in invites if (i or {}).get("master_user_id") != master.id
        ]
        state["sub_user_join_attempts"] = 0
        state["sub_user_join_locked_until"] = None

        await store.async_save(state)
        for url_path in dict.fromkeys(to_reconcile):
            await _reconcile_dashboard_visibility(hass, url_path, state)

        _LOGGER.info(
            "household reset by master %s — removed %d sub-user(s)",
            master.id,
            len(removed),
        )
        return self.json({"status": "ok", "removed": removed, "skipped": skipped})


class GASiteResetRequestView(HomeAssistantView):
    """Master-only: file a tenant-wipe request for the ga_manager addon.

    Returns 202 — accepted, not done. The addon validates the marker again
    before it acts, and the wipe itself stops Core, so the caller will lose
    this connection mid-way. That is expected; the UI has to say so.
    """

    url = "/api/greenautarky_site/site_reset/request"
    name = "api:greenautarky_site:site_reset_request"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Validate, then drop the marker file."""
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        if not _confirm_ok(body):
            return web.json_response(
                {"message": f"Confirmation phrase required ({RESET_CONFIRM_PHRASE})"},
                status=400,
            )

        # Physical presence, checked NOW — not the sticky onboarding flag.
        if err := await async_verify_pin_fresh(
            hass, body.get("pin") or "", scope=RESET_PIN_SCOPE
        ):
            return err

        now = datetime.now(UTC)
        payload = {
            "schema_version": RESET_REQUEST_SCHEMA_VERSION,
            "kind": "tenant-wipe",
            "nonce": secrets.token_hex(16),
            "requested_at": now.isoformat(),
            # The WRITER states the deadline rather than letting the addon
            # derive it: the two sides can then disagree about the TTL without
            # disagreeing about whether a given marker is still live.
            "expires_at": (
                now + timedelta(seconds=RESET_REQUEST_TTL_S)
            ).isoformat(),
            "requested_by": {
                "ha_user_id": master.id,
                "name": getattr(master, "name", None),
            },
            "reason": f"resident self-service reset (master {master.id})",
            # Sensors stay paired by default: the hardware belongs to the home,
            # not to the departing tenant, and re-pairing a house full of
            # devices is not something a resident should trigger by accident.
            "wipe_zigbee_pairing": bool(body.get("wipe_zigbee_pairing")),
        }

        path = _request_path(hass)
        try:
            await hass.async_add_executor_job(_write_request, path, payload)
        except OSError as e:
            _LOGGER.error("site reset: could not write %s: %s", path, e)
            return web.json_response(
                {"message": "Could not file the reset request"}, status=500
            )

        _LOGGER.warning(
            "site reset REQUESTED by master %s (nonce %s) — ga_manager will "
            "pick it up within its poll interval",
            master.id,
            payload["nonce"],
        )
        return self.json(
            {"status": "accepted", "nonce": payload["nonce"]}, status_code=202
        )


class GASiteResetStatusView(HomeAssistantView):
    """Master-only: what has the addon done with the request?

    ``pending`` means the marker is still on disk (the addon has not picked it
    up yet). ``status`` is whatever ga_manager last wrote — it survives the
    marker, so the card can still show an outcome after pickup.
    """

    url = "/api/greenautarky_site/site_reset/status"
    name = "api:greenautarky_site:site_reset_status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Report marker presence + the addon's last status write."""
        hass: HomeAssistant = request.app["hass"]
        _, err = await _require_master(request)
        if err:
            return err

        pending, status = await hass.async_add_executor_job(
            _read_pending_and_status, _request_path(hass), _status_path(hass)
        )
        return self.json({"pending": pending, "status": status})


def _read_pending_and_status(
    request_path: Path, status_path: Path
) -> tuple[bool, dict[str, Any] | None]:
    """Both disk reads in one executor hop (SYNC)."""
    return request_path.exists(), _read_json(status_path)
