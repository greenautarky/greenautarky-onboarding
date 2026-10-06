"""Where the device and E2E tiers reach Home Assistant, and which OAuth client_id
they log in with. One place, because eight modules derived both from
``GA_DEVICE_URL`` by string concatenation and all eight broke the same way.

WHY ``CLIENT_ID`` IS NOT SIMPLY ``GA_DEVICE_URL + "/"``

Home Assistant's ``/auth/login_flow`` rejects a client_id whose host is an IP
address outside loopback / RFC 1918 / link-local
(``homeassistant/components/auth/indieauth.py`` ``_parse_client_id`` →
``homeassistant.util.network.is_local``). A canary is reached over its NetBird
mesh address, which is 100.64.0.0/10 (CGNAT) — not "local" to Core. The check
parses ``urlparse(client_id).netloc`` with ``ip_address()``; a netloc that
carries a port (``100.x.y.z:8123``) is not an IP literal, so it is treated as a
host name and accepted. That is why this only appeared when Core moved to port
80 (ADR-0038): ``http://<mesh-ip>/`` has a bare-IP netloc and is answered
``{"message": "Invalid client id"}``.

So the client_id always names the port explicitly — ``http://<host>:80/``. It is
still the URL the client talks to; Core requires redirect_uri to share its
scheme and netloc, and every caller here passes the same string for both.
``tests/test_device_target.py`` pins this against Core's own
``verify_client_id``, so a Core that stops accepting it fails the unit suite
instead of the next release train.

WHY ``DEVICE_URL`` DROPS A DEFAULT PORT

The browser tier authenticates by writing ``hassTokens`` into localStorage.
home-assistant-js-websocket's ``getAuth`` ignores stored tokens whose
``hassUrl`` differs from the page's ``location.protocol + "//" + location.host``
("If the token is for another url, ignore it"), and a browser never shows
``:80`` in ``location.host``. With ``hassUrl = "http://<ip>:80"`` the frontend
discards the token, redirects to ``/auth/authorize`` with its own client_id
``http://<ip>/`` — the rejected form above — and the page never gets a
``hass`` object. So ``DEVICE_URL`` is the canonical origin, which is also what
``page.wait_for_url`` compares against.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_origin(url: str) -> str:
    """``scheme://host[:port]`` as a browser writes it: default port dropped,
    no path, no trailing slash. Empty in, empty out."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"GA_DEVICE_URL must be http(s)://host[:port], got {url!r}")
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def client_id_for(url: str) -> str:
    """The OAuth client_id for a device URL: same host, port always explicit."""
    origin = canonical_origin(url)
    if not origin:
        return "http://device/"
    parts = urlsplit(origin)
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    return f"{parts.scheme}://{host}:{port}/"


DEVICE_URL = canonical_origin(os.environ.get("GA_DEVICE_URL", ""))
CLIENT_ID = client_id_for(DEVICE_URL)
