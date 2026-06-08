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
    from greenautarky_onboarding import async_setup

    # The default ``hass`` fixture builds a minimal HA instance without
    # the HTTP component. We inject a stub so the ``register_view``
    # calls in ``_async_setup_common`` don't AttributeError.
    if not hasattr(hass, "http") or hass.http is None:
        hass.http = MagicMock()  # noqa: SLF001 — test-only setattr

    with (
        patch(
            "greenautarky_onboarding._async_register_frontend_bundle",
            return_value=None,
        ),
        patch(
            "greenautarky_onboarding._async_register_panel",
            return_value=None,
        ),
        patch(
            "greenautarky_onboarding._register_redirect_js",
            return_value=None,
        ),
        patch(
            "greenautarky_onboarding._patch_index_view_for_wizard_redirect",
            return_value=None,
        ),
    ):
        ok = await async_setup(hass, {"greenautarky_onboarding": {}})
    assert ok is True


@pytest.mark.asyncio
async def test_module_imports() -> None:
    """Cheapest possible check: the package + all submodules import
    cleanly. Catches ``from foo import bar`` typos that pyflakes/ruff
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
