"""Personal dashboards — auto-created per (sub-)user (ADR-0006 matrix).

Every tenant user created through this component (the auto-elected master
in the onboarding account step, and every sub-user joining via invite)
gets a personal storage dashboard. The dashboard is then assigned to the
user in the ``sub_user_dashboards`` matrix so the existing per-view
``visible`` reconcile hides it from everyone else (masters keep access).

Why these dashboards are COMPONENT-OWNED
-----------------------------------------
Lovelace's ``DashboardsCollection`` (what Settings → Dashboards manages)
is a local variable inside ``lovelace.async_setup`` — it is not reachable
from a custom component. Instantiating a second collection over the same
storage key is not an option either: ``StorageCollection`` persists its
whole in-memory dict on every save, so two instances last-write-wins
clobber each other and user-created dashboards would silently vanish.

So we do what lovelace itself does for YAML dashboards: create the
``LovelaceStorage`` object and register the frontend panel ourselves, at
creation time and again on every boot (from our own state store, key
``personal_dashboards``). Trade-off, documented on purpose: personal
dashboards do not appear in Settings → Dashboards; they are managed
through the GA master console / matrix instead.

Best-effort throughout: a failure here must never break user creation.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

# url_path prefix. Must contain a hyphen (lovelace requires one so storage
# dashboards can never shadow built-in single-word panels).
URL_PREFIX = "ga-home"

# Starter view — German, matching the rest of the customer-facing surface.
_STARTER_CARD = (
    "## Willkommen, {name}!\n\n"
    "Das ist dein persönliches Dashboard. Dein Haushalts-Verwalter kann dir "
    "hier weitere Ansichten freigeben — und du kannst es selbst anpassen "
    "(Stift-Symbol oben rechts)."
)


def _starter_config(name: str) -> dict[str, Any]:
    """Initial dashboard config for a new personal dashboard."""
    return {
        "views": [
            {
                "title": name,
                "path": "home",
                "cards": [
                    {
                        "type": "markdown",
                        "content": _STARTER_CARD.format(name=name),
                    }
                ],
            }
        ]
    }


def personal_dashboards(state: dict[str, Any]) -> dict[str, str]:
    """The user_id → url_path map of component-owned personal dashboards."""
    dashboards = state.get("personal_dashboards")
    return dashboards if isinstance(dashboards, dict) else {}


def _unique_url_path(hass: HomeAssistant, name: str, user_id: str) -> str:
    """Derive a free url_path from the display name (user-id fallback)."""
    panels = hass.data.get(frontend.DATA_PANELS) or {}
    slug = slugify(name)[:24].strip("_-") or user_id[:8]
    candidate = f"{URL_PREFIX}-{slug}"
    if candidate not in panels:
        return candidate
    for suffix in range(2, 10):
        candidate = f"{URL_PREFIX}-{slug}-{suffix}"
        if candidate not in panels:
            return candidate
    return f"{URL_PREFIX}-{user_id[:12]}"


async def _register(
    hass: HomeAssistant, url_path: str, title: str, *, seed_config: bool
) -> None:
    """Create the LovelaceStorage + frontend panel for one dashboard.

    ``seed_config`` writes the starter view — only on first creation, never
    on boot re-registration (the user's own edits live in the dashboard's
    config store and must survive).
    """
    # Imported lazily: lovelace is always set up on GA OS before we run
    # (we re-register on EVENT_HOMEASSISTANT_STARTED), but unit tests may
    # build a hass without it.
    from homeassistant.components.lovelace import dashboard as lovelace_dashboard
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    item = {
        "id": f"ga_personal_{url_path.removeprefix(URL_PREFIX + '-')}",
        "url_path": url_path,
        "title": title,
        "icon": "mdi:account-circle",
        "require_admin": False,
        "show_in_sidebar": True,
        "mode": "storage",
    }
    store_obj = lovelace_dashboard.LovelaceStorage(hass, item)

    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        raise RuntimeError("lovelace is not set up")
    lovelace_data.dashboards[url_path] = store_obj

    if seed_config:
        await store_obj.async_save(_starter_config(title))

    if url_path not in (hass.data.get(frontend.DATA_PANELS) or {}):
        frontend.async_register_built_in_panel(
            hass,
            "lovelace",
            config={"mode": "storage"},
            frontend_url_path=url_path,
            require_admin=False,
            sidebar_title=title,
            sidebar_icon="mdi:account-circle",
        )


async def async_create_personal_dashboard(
    hass: HomeAssistant, state: dict[str, Any], user_id: str, name: str
) -> str | None:
    """Create + register a personal dashboard for ``user_id``.

    Records it in ``state["personal_dashboards"]`` and assigns it in the
    ``sub_user_dashboards`` matrix. The caller persists the state store and
    runs the visibility reconcile. Returns the url_path, or None on failure
    (best-effort — never raises).
    """
    existing = personal_dashboards(state).get(user_id)
    if existing:
        return existing
    try:
        url_path = _unique_url_path(hass, name, user_id)
        await _register(hass, url_path, name, seed_config=True)
    except Exception as err:
        _LOGGER.warning(
            "personal dashboard for %s (%s) could not be created: %s",
            name,
            user_id,
            err,
        )
        return None

    state.setdefault("personal_dashboards", {})[user_id] = url_path
    matrix = state.setdefault("sub_user_dashboards", {})
    assigned = set(matrix.get(user_id) or [])
    assigned.add(url_path)
    matrix[user_id] = sorted(assigned)
    _LOGGER.info("created personal dashboard %s for user %s", url_path, user_id)
    return url_path


async def async_register_all(hass: HomeAssistant, state: dict[str, Any]) -> None:
    """Re-register every known personal dashboard (boot path).

    Storage panels registered at runtime are gone after a restart; lovelace
    re-registers only its collection's dashboards, so ours must be re-added
    from our state store. Never seeds config (user edits are durable in the
    dashboard's own store). Best-effort per dashboard.
    """
    for user_id, url_path in personal_dashboards(state).items():
        try:
            user = await hass.auth.async_get_user(user_id)
            if user is None or not user.is_active:
                continue
            await _register(hass, url_path, user.name or url_path, seed_config=False)
        except Exception as err:
            _LOGGER.warning(
                "personal dashboard %s (user %s) could not be re-registered: %s",
                url_path,
                user_id,
                err,
            )


async def async_delete_personal_dashboard(
    hass: HomeAssistant, state: dict[str, Any], user_id: str
) -> str | None:
    """Delete ``user_id``'s personal dashboard entirely (panel + config +
    bookkeeping). Used by sub-user removal: without this the orphaned board
    survived AND the matrix reconcile stripped its per-view ``visible`` —
    making the removed user's private board visible to EVERYONE (KB #149
    §5a). Best-effort; returns the deleted url_path or None."""
    url_path = personal_dashboards(state).pop(user_id, None)
    if not url_path:
        return None
    (state.get("sub_user_dashboards") or {}).pop(user_id, None)
    try:
        if url_path in (hass.data.get(frontend.DATA_PANELS) or {}):
            frontend.async_remove_panel(hass, url_path)
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is not None:
            dash = lovelace_data.dashboards.pop(url_path, None)
            if dash is not None and hasattr(dash, "async_delete"):
                await dash.async_delete()
    except Exception as err:  # noqa: BLE001 — removal must not fail on cleanup
        _LOGGER.warning(
            "personal dashboard %s (user %s) cleanup incomplete: %s",
            url_path,
            user_id,
            err,
        )
    _LOGGER.info("deleted personal dashboard %s of removed user %s", url_path, user_id)
    return url_path
