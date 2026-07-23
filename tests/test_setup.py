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


async def test_legacy_storage_file_is_moved_on_setup(hass) -> None:
    """Rename migration (#574): a device provisioned as `greenautarky_onboarding`
    must carry its WHOLE household state over to the new key on first boot —
    completed flag, consents, sub_users — and the old file must be GONE afterwards
    (a stale copy would hold personal data the tenant-wipe can no longer see)."""
    import json as _json
    from pathlib import Path as _Path

    from greenautarky_site import _migrate_legacy_storage_file
    from greenautarky_site.const import STORAGE_KEY

    legacy = _Path(hass.config.path(".storage", "greenautarky_onboarding"))
    new = _Path(hass.config.path(".storage", STORAGE_KEY))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    # the hass fixture shares its config dir across tests — start clean
    new.unlink(missing_ok=True)
    legacy.unlink(missing_ok=True)
    legacy.write_text(_json.dumps({
        "version": 2, "minor_version": 1, "key": "greenautarky_onboarding",
        "data": {"completed": True, "consents": {"gdpr": {"version": 1}},
                 "sub_users": {"u9": {"parent": "m1"}}},
    }))

    assert await hass.async_add_executor_job(_migrate_legacy_storage_file, hass) is True

    assert not legacy.exists()
    stored = _json.loads(new.read_text())
    assert stored["key"] == STORAGE_KEY
    assert stored["data"]["completed"] is True
    assert stored["data"]["sub_users"] == {"u9": {"parent": "m1"}}

    # idempotent: second call is a no-op (new exists, old gone)
    assert await hass.async_add_executor_job(_migrate_legacy_storage_file, hass) is False


async def test_no_legacy_file_migration_is_noop(hass) -> None:
    """A fresh device (no legacy file) must not invent state."""
    from pathlib import Path as _Path

    from greenautarky_site import _migrate_legacy_storage_file
    from greenautarky_site.const import STORAGE_KEY

    # the hass fixture shares its config dir across tests — start clean
    for key in (STORAGE_KEY, "greenautarky_onboarding"):
        _Path(hass.config.path(".storage", key)).unlink(missing_ok=True)

    assert await hass.async_add_executor_job(_migrate_legacy_storage_file, hass) is False
    assert not _Path(hass.config.path(".storage", STORAGE_KEY)).exists()
