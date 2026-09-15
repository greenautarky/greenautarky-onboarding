"""The stock-panel sweep, asserted on the panel registry — not on the call.

WHY THIS FILE EXISTS
--------------------
``_hide_default_ha_panels`` used to run exactly twice: once inside setup, and
once more on ``EVENT_HOMEASSISTANT_STARTED``. Its docstring named ``todo`` as
the very reason for the second run. Measured on a canary on 2026-09-15, four of
the six listed panels were gone and ``map`` and ``todo`` were still in the
sidebar — the two it had been extended for.

The cause is neither startup ordering nor a rename. Both panels are registered
by HOME ASSISTANT'S OWN ONBOARDING COMPLETION, which on a GA device happens
while the resident walks through our wizard — long after the last sweep:

  * ``POST /api/onboarding/core_config`` creates a ``shopping_list`` config
    entry (``onboarding/views.py``: ``onboard_integrations``). Setting that
    entry up forwards the ``todo`` platform, whose ``async_setup`` calls
    ``frontend.async_register_built_in_panel(hass, "todo", ...)``.
  * ``lovelace.async_setup`` registers ``create_map_dashboard`` as an
    onboarding listener while the instance is not onboarded. Completing
    onboarding fires it, the ``map`` dashboard lands in the dashboards
    collection, and the collection listener registers the ``map`` panel.

Four panels (``energy``, ``logbook``, ``history``, ``media-browser``) arrive via
``default_config`` at boot, which is why the FIRST sweep caught them and the
defect looked like "two panels are special".

So the defect class is: a ONE-SHOT mutation of a registry that other
integrations keep writing to. Any fixed number of sweeps loses the same race at
the next moment somebody registers a panel. The fix keeps the invariant instead
of re-running the mutation, and the tests below assert the REGISTRY CONTENTS at
each of those moments rather than that a function was called.

The expected values here are PINNED LITERALS on purpose. Deriving them from
``GA_HIDDEN_DEFAULT_PANELS`` would make the audit agree with whatever the
component happens to declare, including a wrong declaration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components import frontend
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant

pytestmark = pytest.mark.asyncio


# The six stock panels the GA tenant flow must not show a resident. Pinned
# here, never imported from the component (rule: an audit may not take its
# expected value from the artifact it audits).
STOCK_PANELS_THAT_MUST_GO = (
    "energy",
    "logbook",
    "history",
    "media-browser",
    "todo",
    "map",
)

# Panels that must SURVIVE. A sweep that removes everything is overridden by
# reflex, which is a slower way of having no sweep at all.
PANELS_THAT_MUST_STAY = (
    "lovelace",
    "config",
    "developer-tools",
    "profile",
    "greenautarky-setup-panel",
    "ga-home-anna",
)


def _register(hass: HomeAssistant, url_path: str, *, title: str | None = None) -> None:
    """Register a panel the way Home Assistant Core does."""
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        sidebar_title=title if title is not None else url_path,
        sidebar_icon="mdi:dots-horizontal",
        frontend_url_path=url_path,
        update=True,
    )


def _panels(hass: HomeAssistant) -> set[str]:
    return set((hass.data.get(frontend.DATA_PANELS) or {}).keys())


def _setup_patches() -> tuple[Any, ...]:
    """Peripheral wiring only — the sweep itself is never patched."""
    return (
        patch("greenautarky_site._async_register_frontend_bundle", return_value=None),
        patch("greenautarky_site._async_register_panel", return_value=None),
        patch("greenautarky_site._register_redirect_js", return_value=None),
        patch("greenautarky_site._patch_index_view_for_wizard_redirect", return_value=None),
    )


async def _boot(hass: HomeAssistant, yaml_config: dict[str, Any] | None = None) -> None:
    """Run the component's real YAML setup path, then fire STARTED.

    Nothing about panels is stubbed: ``_hide_default_ha_panels`` and everything
    it installs runs for real.
    """
    from greenautarky_site import async_setup

    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()

    config: dict[str, Any] = {"greenautarky_site": yaml_config}
    a, b, c, d = _setup_patches()
    with a, b, c, d:
        assert await async_setup(hass, config) is True
    await hass.async_block_till_done()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


# ─────────────────────────────────────────────────────────────────────────────
# The defect, at the three moments a panel can be registered
# ─────────────────────────────────────────────────────────────────────────────


async def test_stock_panels_registered_before_setup_are_gone(hass) -> None:
    """``default_config``'s panels: registered at boot, before we set up."""
    for name in STOCK_PANELS_THAT_MUST_GO:
        _register(hass, name)

    await _boot(hass)

    assert _panels(hass) & set(STOCK_PANELS_THAT_MUST_GO) == set()


async def test_stock_panels_registered_between_setup_and_started_are_gone(hass) -> None:
    """A stage-2 integration that lands after us but before STARTED."""
    from greenautarky_site import async_setup

    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()

    a, b, c, d = _setup_patches()
    with a, b, c, d:
        assert await async_setup(hass, {"greenautarky_site": None}) is True
    await hass.async_block_till_done()

    for name in STOCK_PANELS_THAT_MUST_GO:
        _register(hass, name)

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert _panels(hass) & set(STOCK_PANELS_THAT_MUST_GO) == set()


async def test_panels_registered_after_started_are_gone(hass) -> None:
    """THE regression test.

    This is HA onboarding completion: ``shopping_list``'s config entry brings
    in the ``todo`` platform (and its panel), and lovelace's onboarding
    listener creates the ``map`` dashboard (and its panel) — both long after
    ``EVENT_HOMEASSISTANT_STARTED`` has come and gone.

    A component that sweeps a fixed number of times cannot pass this.
    """
    await _boot(hass)

    # …the resident finishes the wizard, which finishes HA onboarding…
    _register(hass, "todo", title="To-do-Listen")
    _register(hass, "map", title="Karte")
    await hass.async_block_till_done()

    still_there = sorted(_panels(hass) & {"todo", "map"})
    assert still_there == [], (
        f"Stock panels still in the sidebar after HA onboarding completed: "
        f"{still_there}. These are registered by "
        f"POST /api/onboarding/core_config (shopping_list -> todo platform) and "
        f"by lovelace's onboarding listener (map dashboard) — i.e. AFTER the "
        f"last sweep. A one-shot sweep cannot hold this invariant."
    )


async def test_every_listed_panel_is_removed_whenever_it_reappears(hass) -> None:
    """Coverage, not exit code: assert on all six, one re-registration each."""
    await _boot(hass)

    survivors: list[str] = []
    for name in STOCK_PANELS_THAT_MUST_GO:
        _register(hass, name)
        await hass.async_block_till_done()
        if name in _panels(hass):
            survivors.append(name)

    assert survivors == [], f"panels that survived re-registration: {survivors}"


# ─────────────────────────────────────────────────────────────────────────────
# Must-NOT-flag: the sweep may not take anything else with it
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_panels_we_keep_are_untouched(hass) -> None:
    """A gate that flags everything gets switched off. Both directions."""
    for name in PANELS_THAT_MUST_STAY:
        _register(hass, name)

    await _boot(hass)

    missing = sorted(set(PANELS_THAT_MUST_STAY) - _panels(hass))
    assert missing == [], f"the sweep removed panels it must keep: {missing}"


async def test_panels_registered_later_that_we_keep_survive(hass) -> None:
    """The listener must not turn into "remove everything that appears"."""
    await _boot(hass)

    for name in PANELS_THAT_MUST_STAY:
        _register(hass, name)
    await hass.async_block_till_done()

    missing = sorted(set(PANELS_THAT_MUST_STAY) - _panels(hass))
    assert missing == [], f"the sweep removed panels it must keep: {missing}"


async def test_unrelated_panel_churn_does_not_loop(hass, caplog) -> None:
    """Registering a panel we do not care about must be a no-op, not a storm.

    ``async_remove_panel`` logs ``Removing unknown panel`` for every name it
    does not find. Calling it unconditionally on every registry change would
    print six warnings per panel registration on a live device.
    """
    await _boot(hass)
    caplog.clear()

    for i in range(5):
        _register(hass, f"some-other-panel-{i}")
    await hass.async_block_till_done()

    assert "Removing unknown panel" not in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# The documented option — promised in a comment since day one, read nowhere
# ─────────────────────────────────────────────────────────────────────────────


async def test_hide_default_panels_false_keeps_every_stock_panel(hass) -> None:
    """``greenautarky_site: hide_default_panels: false`` must actually work.

    The code comment has promised this switch since the sweep was written and
    nothing ever read the key: the sweep ran unconditionally. A documented
    switch that does nothing is worse than no switch, because the next person
    turns it and concludes the component is broken.
    """
    for name in STOCK_PANELS_THAT_MUST_GO:
        _register(hass, name)

    await _boot(hass, {"hide_default_panels": False})

    missing = sorted(set(STOCK_PANELS_THAT_MUST_GO) - _panels(hass))
    assert missing == [], (
        f"hide_default_panels: false still removed {missing} — the option is "
        f"documented as the way back without a redeploy."
    )

    # …and it must stay off for later registrations too.
    _register(hass, "todo")
    await hass.async_block_till_done()
    assert "todo" in _panels(hass)


async def test_hide_default_panels_defaults_to_on(hass) -> None:
    """Absent key = current behaviour. The opt-out must be explicit."""
    for name in STOCK_PANELS_THAT_MUST_GO:
        _register(hass, name)

    await _boot(hass, {})

    assert _panels(hass) & set(STOCK_PANELS_THAT_MUST_GO) == set()


async def test_config_schema_accepts_the_documented_key(caplog) -> None:
    """The bare key AND the documented key must both validate, quietly.

    ``cv.empty_config_schema`` does not raise — it logs
    ``The greenautarky_site integration does not support any configuration
    parameters`` at ERROR and hands the config on. So an operator who followed
    the comment got a config ERROR in the log telling them the key does not
    exist, plus a sweep that ran anyway. Measured, not recalled: this test was
    written asserting a ValueError and the red run proved otherwise.

    ga_manager's converge writes the BARE key, so that must keep working.
    """
    from greenautarky_site import CONFIG_SCHEMA

    caplog.clear()
    CONFIG_SCHEMA({"greenautarky_site": None})
    CONFIG_SCHEMA({"greenautarky_site": {}})
    CONFIG_SCHEMA({"greenautarky_site": {"hide_default_panels": False}})
    CONFIG_SCHEMA({"greenautarky_site": {"hide_default_panels": True}})

    assert "does not support any configuration parameters" not in caplog.text


async def test_config_schema_rejects_an_unknown_key() -> None:
    """A typo must fail loudly rather than be silently ignored."""
    import voluptuous as vol

    from greenautarky_site import CONFIG_SCHEMA

    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({"greenautarky_site": {"hide_defualt_panels": False}})
