"""The personal dashboard must not be a sidebar entry — and must still work.

Measured on a canary on 2026-09-15: a resident's sidebar carried a panel whose
name embeds their own username (``ga-home-<slug>``). That panel is not a stock
HA leak — WE register it, in ``dashboards._register``, with a sidebar title and
an icon. So the panel sweep is the wrong tool entirely: the fix is to stop
putting it in the sidebar, not to remove it again afterwards.

WHAT MUST NOT BREAK, and why it is easy to break it
---------------------------------------------------
Two different things share the ``ga-home`` prefix and they are unrelated:

  * ``ga-home`` — the dashboard STRATEGY (ga-frontend-bundle). HA's default
    Overview ("Übersicht", ``url_path`` None, storage key ``lovelace``) renders
    through it. It is not a panel at all.
  * ``ga-home-<slug>`` — a per-user storage dashboard owned by this component
    (ADR-0006), one per master/sub-user.

A pattern written as ``ga-home*`` therefore reads as if it covered the
Übersicht. It does not, and the Übersicht must keep rendering either way — so
that is asserted here explicitly rather than assumed.

The dashboard itself stays fully alive: its ``LovelaceStorage`` is registered,
its config loads, and ``/ga-home-<slug>`` still resolves. HA's frontend drops a
panel from the sidebar when its ``title`` is falsy
(``ha-sidebar.ts``/``computePanels``: ``!panel.title`` → skip), which is the
oldest and most portable of the three filters there — ``show_in_sidebar`` and
``default_visible`` are newer.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from homeassistant.components import frontend
from homeassistant.components.lovelace.const import LOVELACE_DATA

from greenautarky_site import dashboards
from greenautarky_site.const import DOMAIN

pytestmark = pytest.mark.asyncio


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


def _seed(hass) -> dict[str, Any]:
    state: dict[str, Any] = {"completed": True, "steps_done": [], "consents": {}}
    hass.data[DOMAIN] = {"store": _FakeStore(), "state": state}
    return state


def _inject_lovelace(hass) -> None:
    """Stand-in for lovelace's runtime data, as test_personal_dashboards.py does.

    The default dashboard (``None``) is seeded so the Übersicht assertion below
    has something real to be about.
    """
    from homeassistant.components.lovelace import dashboard as lovelace_dashboard

    hass.data[LOVELACE_DATA] = types.SimpleNamespace(
        dashboards={None: lovelace_dashboard.LovelaceStorage(hass, None)}
    )
    hass.data.setdefault(frontend.DATA_PANELS, {})


async def test_personal_dashboard_is_not_a_sidebar_entry(hass) -> None:
    """The panel must carry no sidebar title — that is what hides it."""
    state = _seed(hass)
    _inject_lovelace(hass)

    url = await dashboards.async_create_personal_dashboard(
        hass, state, "uid-1", "Anna Schmidt"
    )

    panel = hass.data[frontend.DATA_PANELS][url]
    assert not panel.sidebar_title, (
        f"panel {url} carries sidebar_title={panel.sidebar_title!r}, so HA's "
        f"frontend puts it in the resident's sidebar (computePanels keeps any "
        f"panel with a truthy title). The per-user board is reached from the "
        f"master console, not from the sidebar."
    )


async def test_personal_dashboard_is_still_reachable_and_renders(hass) -> None:
    """Must-not-flag: hiding the sidebar entry may not disable the board.

    The panel has to stay REGISTERED (otherwise ``/ga-home-<slug>`` 404s) and
    its Lovelace store has to keep its config (otherwise the board is blank).
    """
    state = _seed(hass)
    _inject_lovelace(hass)

    url = await dashboards.async_create_personal_dashboard(
        hass, state, "uid-1", "Anna Schmidt"
    )

    assert url in hass.data[frontend.DATA_PANELS], "the URL must still resolve"
    assert url in hass.data[LOVELACE_DATA].dashboards
    config = await hass.data[LOVELACE_DATA].dashboards[url].async_load(False)
    assert "Willkommen, Anna Schmidt" in str(config)
    assert state["personal_dashboards"] == {"uid-1": url}


async def test_boot_reregistration_also_keeps_it_out_of_the_sidebar(hass) -> None:
    """The boot path is a second registration site — it must agree.

    ``async_register_all`` re-registers every known board on every start. A fix
    applied only at creation time would be undone by the next reboot, which is
    exactly the shape of defect that survives a hand-test.
    """
    state = _seed(hass)
    _inject_lovelace(hass)
    user = await hass.auth.async_create_user("Anna Schmidt")
    state["personal_dashboards"] = {user.id: "ga-home-anna_schmidt"}

    hass.data[frontend.DATA_PANELS].clear()
    await dashboards.async_register_all(hass, state)

    panel = hass.data[frontend.DATA_PANELS]["ga-home-anna_schmidt"]
    assert not panel.sidebar_title
    assert "ga-home-anna_schmidt" in hass.data[LOVELACE_DATA].dashboards


async def test_the_uebersicht_is_untouched(hass) -> None:
    """The default dashboard is a different object and must stay put.

    ``ga-home`` (the strategy the Übersicht renders) and ``ga-home-<slug>``
    (a per-user board) share a prefix and nothing else. Asserted so a future
    prefix-matching change cannot quietly take the Übersicht with it.
    """
    state = _seed(hass)
    _inject_lovelace(hass)
    frontend.async_register_built_in_panel(
        hass, "lovelace", sidebar_title="Übersicht", frontend_url_path="lovelace"
    )

    await dashboards.async_create_personal_dashboard(
        hass, state, "uid-1", "Anna Schmidt"
    )

    assert None in hass.data[LOVELACE_DATA].dashboards, "default dashboard gone"
    uebersicht = hass.data[frontend.DATA_PANELS]["lovelace"]
    assert uebersicht.sidebar_title == "Übersicht"
