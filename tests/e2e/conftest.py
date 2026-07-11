"""Device-tier fixtures: re-allow the real device host.

pytest-homeassistant-custom-component's ``pytest_runtest_setup`` hook pins
pytest_socket to 127.0.0.1 and swaps in a guarded socket class before every
test. The device tier talks to a REAL device, so undo both — ORDER MATTERS:
``enable_socket()`` first (restores the true socket class), THEN
``socket_allow_hosts`` (installs the allow-list guard on that class; the
reverse order would install the guard on the about-to-be-discarded class).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest


@pytest.fixture(autouse=True)
def _allow_device_host():
    host = urlparse(os.environ.get("GA_DEVICE_URL", "")).hostname
    if host:
        import pytest_socket

        pytest_socket.enable_socket()
        pytest_socket.socket_allow_hosts(
            ["127.0.0.1", host], allow_unix_socket=True
        )
    yield
