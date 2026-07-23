"""Integration smoke test — does our integration even set up?

These are the cheap "did we leave a syntax error in __init__.py"
checks. Heavier flow tests for the wizard, consent, password reset,
and console-login views live in their own files (next iteration —
they need ``hass.http`` fully wired which requires more than the
default ``hass`` fixture from ``pytest-homeassistant-custom-component``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_async_setup_returns_true(hass) -> None:
    """``async_setup`` with an empty config dict must return True.

    The full setup touches ``hass.http`` (view registration) +
    ``frontend``-component data structures + ``Store`` IO. For the
    smoke test we patch the panel/JS-injection helpers, install a
    mock ``hass.http``, and verify the return code — the goal here
    is "did __init__.py import + call without throwing", NOT "is
    everything actually wired up". Wire-up tests need a richer
    fixture and live in test_setup_integration.py (TODO).
    """
    from greenautarky_site import async_setup

    # The default ``hass`` fixture builds a minimal HA instance without
    # the HTTP component. We inject a stub so the ``register_view``
    # calls in ``_async_setup_common`` don't AttributeError.
    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()

    with (
        patch(
            "greenautarky_site._async_register_frontend_bundle",
            return_value=None,
        ),
        patch(
            "greenautarky_site._async_register_panel",
            return_value=None,
        ),
        patch(
            "greenautarky_site._register_redirect_js",
            return_value=None,
        ),
        patch(
            "greenautarky_site._patch_index_view_for_wizard_redirect",
            return_value=None,
        ),
    ):
        ok = await async_setup(hass, {"greenautarky_site": {}})
    assert ok is True


@pytest.mark.asyncio
async def test_module_imports() -> None:
    """Cheapest possible check: the package + all submodules import
    cleanly. Catches ``from foo import bar`` typos that pyflakes/ruff
    might miss (e.g. circular imports)."""
    import importlib

    for mod_name in (
        "greenautarky_site",
        "greenautarky_site.const",
        "greenautarky_site.consent",
        "greenautarky_site.consent_views",
        "greenautarky_site.console_login",
        "greenautarky_site.store",
        "greenautarky_site.onboarding",
        "greenautarky_site.onboarding.wizard",
        "greenautarky_site.onboarding.pin",
        "greenautarky_site.onboarding.password_reset",
        "greenautarky_site.household",
        "greenautarky_site.household.masters",
        "greenautarky_site.household.sub_users",
        "greenautarky_site.household.dashboards_admin",
        "greenautarky_site.scoping",
        "greenautarky_site.scoping.rooms",
        "greenautarky_site.scoping.entity_scope",
        "greenautarky_site.scoping.leak_guard",
        "greenautarky_site.repairs",
    ):
        importlib.import_module(mod_name)

# ─── rename migration (#574): legacy storage key → greenautarky_site ──────


def _setup_patches():
    from unittest.mock import patch

    return (
        patch("greenautarky_site._async_register_frontend_bundle", return_value=None),
        patch("greenautarky_site._async_register_panel", return_value=None),
        patch("greenautarky_site._register_redirect_js", return_value=None),
        patch("greenautarky_site._patch_index_view_for_wizard_redirect", return_value=None),
    )


async def test_full_setup_adopts_legacy_storage(hass, hass_storage) -> None:
    """THE regression test for the K0 2026-07-23 data-loss bug.

    A device provisioned as `greenautarky_onboarding` must carry its WHOLE
    household state (completed, consents, sub_users, sub_user_areas) to the new
    key — via the real async_setup path AND through the Store API. The first
    migration cut moved the file on the FILESYSTEM and its unit test passed,
    but HA's store manager caches the .storage dir listing at boot, so
    Store.async_load reported the moved file as absent → fresh default state →
    sub-user maps silently lost on K0. Never bypass the Store API.
    """
    from unittest.mock import MagicMock

    from greenautarky_site import async_setup
    from greenautarky_site.const import DOMAIN, STORAGE_KEY

    hass_storage["greenautarky_onboarding"] = {
        "version": 2, "minor_version": 1, "key": "greenautarky_onboarding",
        "data": {"completed": True, "gdpr_accepted": True, "steps_done": ["pin"],
                 "consents": {"gdpr": {"version": 1}},
                 "sub_users": {"maxid": {"parent": "annaid"}},
                 "sub_user_areas": {"maxid": ["wohnzimmer"]}},
    }

    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()
    p1, p2, p3, p4 = _setup_patches()
    with p1, p2, p3, p4:
        assert await async_setup(hass, {DOMAIN: {}}) is True
    await hass.async_block_till_done()

    state = hass.data[DOMAIN]["state"]
    assert state.get("sub_users") == {"maxid": {"parent": "annaid"}}
    assert state.get("sub_user_areas") == {"maxid": ["wohnzimmer"]}
    assert state.get("completed") is True
    assert state.get("consents", {}).get("gdpr", {}).get("version") == 1
    # exactly one source of truth afterwards: new key persisted, legacy REMOVED
    assert hass_storage[STORAGE_KEY]["data"]["sub_users"] == {"maxid": {"parent": "annaid"}}
    assert "greenautarky_onboarding" not in hass_storage


async def test_setup_without_legacy_storage_is_unchanged(hass, hass_storage) -> None:
    """No legacy store → the pre-existing-device default branch as before."""
    from unittest.mock import MagicMock

    from greenautarky_site import async_setup
    from greenautarky_site.const import DOMAIN

    hass_storage.pop("greenautarky_onboarding", None)

    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()
    p1, p2, p3, p4 = _setup_patches()
    with p1, p2, p3, p4:
        assert await async_setup(hass, {DOMAIN: {}}) is True

    state = hass.data[DOMAIN]["state"]
    assert state.get("completed") is True  # pre-existing-device default
    assert "sub_users" not in state
