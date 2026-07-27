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
from pytest_homeassistant_custom_component.common import get_test_config_dir


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Auto-yield so every test sees our component as a known custom_component."""
    yield


@pytest.fixture(autouse=True)
def clean_shared_config_dir():
    """Remove GA-owned files from the harness' config dir around every test.

    ``pytest-homeassistant-custom-component``'s config dir lives INSIDE the
    installed package (``…/site-packages/pytest_homeassistant_custom_component/
    testing_config``), so it is shared by every test in a session and survives
    across sessions, temp dirs and branches. Anything a test writes there is
    still there for the next one.

    That bit for real: a PIN file written by the Danger-Zone tests made
    ``_pin_required`` true for a later module, whose create-user call then got
    a 403 "PIN verification required". Worse, the leftover made the failure
    look pre-existing when re-checked on a pristine tree — the file was still
    on disk from the previous run.
    """
    from greenautarky_site.const import (
        MASTER_USERS_FILE,
        PIN_FILE,
        RESET_REQUEST_FILE,
        RESET_STATUS_FILE,
    )

    root = Path(get_test_config_dir())
    owned = (PIN_FILE, MASTER_USERS_FILE, RESET_REQUEST_FILE, RESET_STATUS_FILE)

    def _clear() -> None:
        for rel in owned:
            (root / rel).unlink(missing_ok=True)

    _clear()
    yield
    _clear()


@pytest.fixture
def integration_root() -> Path:
    """Path to the installed-in-repo copy of the integration code.

    Useful for tests that read the .html / .json files directly.
    """
    return Path(__file__).resolve().parent.parent / "src" / "greenautarky_site"
