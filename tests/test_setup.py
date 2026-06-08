"""Integration smoke test — does our integration even set up?

These are the cheap "did we leave a syntax error in __init__.py"
checks. Heavier flow tests for the wizard, consent, password reset,
and console-login views live in their own files (next iteration).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_async_setup_returns_true(hass) -> None:
    """`async_setup` with an empty config dict must return True — the
    integration's bare-name `greenautarky_onboarding:` entry in
    `configuration.yaml` is supposed to register HTTP views and exit
    cleanly without any further config."""
    from greenautarky_onboarding import async_setup

    # The wizard panel registration touches `panel_custom` which needs
    # `frontend` set up. For the smoke test we patch the panel + JS
    # injection paths so we focus on the setup-returns-True invariant.
    with patch(
        "greenautarky_onboarding._async_register_frontend_bundle",
        return_value=None,
    ), patch(
        "greenautarky_onboarding._async_register_panel",
        return_value=None,
    ), patch(
        "greenautarky_onboarding._register_redirect_js",
        return_value=None,
    ), patch(
        "greenautarky_onboarding._patch_index_view_for_wizard_redirect",
        return_value=None,
    ):
        ok = await async_setup(hass, {"greenautarky_onboarding": {}})
    assert ok is True


@pytest.mark.asyncio
async def test_module_imports() -> None:
    """Cheapest possible check: the package + all submodules import
    cleanly. Catches `from foo import bar` typos that pyflakes/ruff
    might miss (e.g. circular imports)."""
    import importlib

    for mod_name in (
        "greenautarky_onboarding",
        "greenautarky_onboarding.const",
        "greenautarky_onboarding.consent",
        "greenautarky_onboarding.http",
        "greenautarky_onboarding.repairs",
    ):
        importlib.import_module(mod_name)
