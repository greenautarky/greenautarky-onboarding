"""Dashboard administration for the household plane — ADR-0006 matrix.

The legacy ``[sub-user x dashboard]`` assignment matrix + the per-view
``visible`` reconcile it drives, the master console prototype page, the
in-process area rename, and the boot-time re-registration/backfill of
personal dashboards. Kept until the last per-user board is migrated to
the room-scoped strategy (see :mod:`..scoping.rooms`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .. import dashboards
from ..store import _get_state, _get_store
from .masters import _read_master_user_ids, _require_master

_LOGGER = logging.getLogger(__name__)

_MASTER_CONSOLE_HTML = (
    Path(__file__).parent.parent / "master_console_page.html"
).read_text(encoding="utf-8")

async def _reconcile_dashboard_visibility(
    hass: HomeAssistant, url_path: str, state: dict[str, Any]
) -> None:
    """Write per-view ``visible`` on a storage dashboard from the matrix.

    Assigned sub-users PLUS all masters are kept visible (so the master can
    still see/manage the board); everyone else is hidden. An empty assignment
    strips ``visible`` (back to visible-to-all). YAML/missing dashboards are
    skipped. Best-effort — failures log, never raise to the caller.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        return
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return
    dash = data.dashboards.get(url_path)
    if dash is None:
        return
    try:
        config = await dash.async_load(False)
    except Exception:
        _LOGGER.debug("reconcile: cannot load dashboard %s (skipped)", url_path)
        return

    matrix = state.get("sub_user_dashboards") or {}
    assigned = {uid for uid, paths in matrix.items() if url_path in (paths or [])}
    views = config.get("views") or []
    if assigned:
        # Assigned sub-users + all masters keep visibility; everyone else hidden.
        masters = await hass.async_add_executor_job(_read_master_user_ids, hass)
        visible_ids = sorted(assigned | masters)
        for view in views:
            view["visible"] = [{"user": uid} for uid in visible_ids]
    else:
        # No sub-users assigned → strip ``visible`` (back to visible-to-all).
        for view in views:
            view.pop("visible", None)
    config["views"] = views
    try:
        await dash.async_save(config)
    except Exception as err:
        _LOGGER.warning("reconcile: cannot save dashboard %s: %s", url_path, err)


def _available_dashboards(hass: HomeAssistant) -> list[dict[str, str]]:
    """List storage/YAML dashboards (url_path + title) for the matrix UI."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        return []
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return []
    out: list[dict[str, str]] = []
    for url_path, cfg in data.dashboards.items():
        if url_path is None:
            continue  # the default dashboard has no addressable url_path
        title = url_path
        item = getattr(cfg, "config", None)
        if isinstance(item, dict) and item.get("title"):
            title = item["title"]
        out.append({"url_path": url_path, "title": title})
    return out


class GASubUserAssignDashboardView(HomeAssistantView):
    """Master-only: assign/unassign a dashboard to one of the master's
    sub-users (the matrix), then reconcile native per-view visibility."""

    url = "/api/greenautarky_site/sub_user/assign_dashboard"
    name = "api:greenautarky_site:sub_user_assign_dashboard"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Update one matrix cell + reconcile the dashboard."""
        # Local import: sub_users imports this module at module level, so
        # the reverse edge must stay lazy to avoid a cycle.
        from .sub_users import _children_of

        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        sub_user_id = (body.get("sub_user_id") or "").strip()
        url_path = (body.get("url_path") or "").strip()
        assigned = bool(body.get("assigned", True))
        if not sub_user_id or not url_path:
            return self.json_message(
                "sub_user_id and url_path are required", status_code=400
            )

        state = _get_state(hass)
        store = _get_store(hass)
        if sub_user_id not in _children_of(state, master.id):
            return web.json_response(
                {"message": "Not your sub-user"}, status=403
            )

        matrix = state.setdefault("sub_user_dashboards", {})
        current = set(matrix.get(sub_user_id, []))
        if assigned:
            current.add(url_path)
        else:
            current.discard(url_path)
        matrix[sub_user_id] = sorted(current)
        await store.async_save(state)

        await _reconcile_dashboard_visibility(hass, url_path, state)
        _LOGGER.info(
            "master %s %s dashboard %s for sub-user %s",
            master.id,
            "assigned" if assigned else "unassigned",
            url_path,
            sub_user_id,
        )
        return self.json(
            {"status": "ok", "dashboards": matrix.get(sub_user_id, [])}
        )


class GASubUserRenameAreaView(HomeAssistantView):
    """Master-only: rename a room (area) via the area registry, in-process."""

    url = "/api/greenautarky_site/sub_user/rename_area"
    name = "api:greenautarky_site:sub_user_rename_area"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Rename an area."""
        hass: HomeAssistant = request.app["hass"]
        _master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        area_id = (body.get("area_id") or "").strip()
        name = (body.get("name") or "").strip()
        if not area_id or not name:
            return self.json_message(
                "area_id and name are required", status_code=400
            )

        from homeassistant.helpers import area_registry as ar

        reg = ar.async_get(hass)
        if reg.async_get_area(area_id) is None:
            return self.json_message("Unknown area_id", status_code=404)
        try:
            reg.async_update(area_id, name=name)
        except ValueError as e:  # duplicate name, etc.
            return self.json_message(str(e), status_code=400)
        _LOGGER.info("area %s renamed to %r", area_id, name)
        return self.json({"status": "ok", "area_id": area_id, "name": name})


class GAMasterConsolePageView(HomeAssistantView):
    """Serve the prototype Master console page.

    The page itself is harmless static HTML; every API call it makes is
    authenticated (it uses the logged-in master's token from localStorage)
    and master-gated server-side. The production UI is a Lovelace custom card
    (ga-frontend-bundle); this served page is the prototype surface.
    """

    url = "/greenautarky-master"
    name = "greenautarky_site:master_console"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the master console HTML."""
        return web.Response(text=_MASTER_CONSOLE_HTML, content_type="text/html")


# ---------------------------------------------------------------------------
# Personal dashboards — boot re-registration + backfill (ADR-0006 matrix)
# ---------------------------------------------------------------------------


async def async_boot_register_personal_dashboards(hass: HomeAssistant) -> None:
    """Re-register personal dashboards on boot + backfill missing ones.

    Runs on EVENT_HOMEASSISTANT_STARTED (lovelace + auth are up by then).

    1. Re-register: storage panels registered at runtime do not survive a
       restart, and lovelace only re-registers its own collection — our
       component-owned dashboards must be re-added from the state store.
    2. Backfill (self-healing, ADR-0006): masters + sub-users that predate
       this feature (or whose creation-time attempt failed) get their
       personal dashboard now. No-op on devices without masters/sub-users.

    Best-effort throughout — must never break component setup.
    """
    state = _get_state(hass)
    await dashboards.async_register_all(hass, state)

    known = dashboards.personal_dashboards(state)
    candidate_ids: set[str] = set()
    try:
        candidate_ids |= await hass.async_add_executor_job(
            _read_master_user_ids, hass
        )
    except Exception as err:
        _LOGGER.debug("personal-dashboard backfill: master read failed: %s", err)
    candidate_ids |= set((state.get("sub_users") or {}).keys())

    changed = False
    for user_id in sorted(candidate_ids - set(known)):
        user = await hass.auth.async_get_user(user_id)
        if user is None or not user.is_active or user.is_admin or user.system_generated:
            continue
        url_path = await dashboards.async_create_personal_dashboard(
            hass, state, user_id, user.name or "Zuhause"
        )
        if url_path:
            changed = True
            await _reconcile_dashboard_visibility(hass, url_path, state)
    if changed:
        await _get_store(hass).async_save(state)
