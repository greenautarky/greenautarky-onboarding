"""The device/E2E tiers' client_id must be one Home Assistant accepts.

Asked of Core's own ``verify_client_id`` / ``verify_redirect_uri`` — never a copy
of its rules — so a Core release that tightens them fails here, in the unit
suite, instead of in the release train's testgate against a canary.
"""

from __future__ import annotations

import pytest
from device_target import canonical_origin, client_id_for
from homeassistant.components.auth.indieauth import (
    verify_client_id,
    verify_redirect_uri,
)

# 100.64.0.0/10 is where every canary's NetBird mesh address lives. Documentation
# addresses would be the polite choice, but the point is the CGNAT range.
MESH = "100.100.1.2"


def test_the_failure_this_exists_for() -> None:
    """Red proof, kept: the old derivation (GA_DEVICE_URL + "/") on a port-80
    device is refused by Core. If this ever passes, the helper is unnecessary."""
    assert not verify_client_id(f"http://{MESH}/")


@pytest.mark.parametrize(
    "device_url",
    [
        f"http://{MESH}",          # Core >= 2026.8 on :80, port implicit
        f"http://{MESH}:80",       # same, port explicit
        f"http://{MESH}:80/",
        f"http://{MESH}:8123",     # Core < 2026.8
        "http://192.168.1.20",     # LAN
        "http://127.0.0.1:18123",  # an SSH local forward
        "http://device.example",
        "https://device.example",
    ],
)
def test_client_id_is_accepted_by_core(device_url: str) -> None:
    cid = client_id_for(device_url)
    assert verify_client_id(cid), cid


@pytest.mark.parametrize("device_url", [f"http://{MESH}", f"http://{MESH}:8123"])
async def test_redirect_uri_equal_to_client_id_is_accepted(device_url: str, hass) -> None:
    """Every caller passes the client_id as its own redirect_uri; Core must take
    that without fetching anything (same scheme + netloc short-circuits)."""
    cid = client_id_for(device_url)
    assert await verify_redirect_uri(hass, cid, cid)
    assert await verify_redirect_uri(hass, cid, cid + "?auth_callback=1")


@pytest.mark.parametrize(
    ("device_url", "origin"),
    [
        (f"http://{MESH}:80", f"http://{MESH}"),
        (f"http://{MESH}:80/", f"http://{MESH}"),
        (f"http://{MESH}", f"http://{MESH}"),
        (f"http://{MESH}:8123", f"http://{MESH}:8123"),
        ("https://device.example:443", "https://device.example"),
        ("", ""),
    ],
)
def test_device_url_is_the_origin_a_browser_reports(device_url: str, origin: str) -> None:
    """hassTokens.hassUrl must equal location.protocol + '//' + location.host,
    and a browser never writes the default port."""
    assert canonical_origin(device_url) == origin


def test_client_id_names_the_port() -> None:
    assert client_id_for(f"http://{MESH}") == f"http://{MESH}:80/"
    assert client_id_for(f"http://{MESH}:8123") == f"http://{MESH}:8123/"


def test_a_url_without_a_scheme_is_refused() -> None:
    with pytest.raises(ValueError):
        canonical_origin(f"{MESH}:80")
