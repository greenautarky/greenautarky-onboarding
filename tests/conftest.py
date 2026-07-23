"""Shared pytest fixtures for greenautarky-site tests.

Most tests need a hass instance that has our integration set up + a
clean Store so they don't bleed state. The ``custom_integrations``
fixture from ``pytest-homeassistant-custom-component`` registers our
package as a discoverable custom_component for the duration of the
test.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Auto-yield so every test sees our component as a known custom_component."""
    yield


@pytest.fixture
def integration_root() -> Path:
    """Path to the installed-in-repo copy of the integration code.

    Useful for tests that read the .html / .json files directly.
    """
    return Path(__file__).resolve().parent.parent / "src" / "greenautarky_site"
