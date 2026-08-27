"""The console-login destination — and the ways it must not be abusable.

WHY THE DESTINATION EXISTS
--------------------------
The console login planted a session and then always went to "/". On a device
whose onboarding is not finished, "/" is the onboarding wizard — so an operator
sent there to look at the ADMIN area had no way past it. `dest` lets the
fleet-manager say where to land.

WHY THESE TESTS ARE THE INTERESTING PART
----------------------------------------
`dest` ends up in `window.location.replace(...)` on a page that has *just*
authenticated the visitor. An unvalidated one is an open redirect on an
authenticated endpoint: hand someone a link, they arrive signed in on a page
you chose. So there are two independent defences, and each is tested on its own:

  1. `dest` travels INSIDE the HMAC-signed token, never as a query parameter
  2. it is validated to a single-slash absolute path even so

Defence 2 exists for the case where defence 1 is bypassed — a wrong or
compromised signer. Testing only the combination would let either rot unnoticed.
"""

from __future__ import annotations

# Loaded BY PATH, not imported as `greenautarky_site.console_dest`: importing it
# through the package would pull in `__init__.py`, which needs Home Assistant.
# Loading the file on its own is also the assertion that this module really has
# no dependencies — if someone adds an import to it, this line stops working and
# the guard stops being testable in a second, which is the property worth
# keeping.
import importlib.util as _ilu
from pathlib import Path

import pytest

_MOD_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "greenautarky_site"
    / "console_dest.py"
)
_spec = _ilu.spec_from_file_location("console_dest", _MOD_PATH)
_console_dest = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_console_dest)

CONSOLE_LOGIN_DEFAULT_DEST = _console_dest.CONSOLE_LOGIN_DEFAULT_DEST
_safe_dest = _console_dest.safe_dest


# ─── accepted ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dest",
    [
        "/",
        "/config",
        "/config/dashboard",
        "/config/integrations",
        "/lovelace/0",
        "/developer-tools/state",
        "/config/dashboard?x=1",
        "/config/dashboard#anchor",
    ],
)
def test_same_origin_paths_are_kept(dest):
    """A single-slash absolute path is exactly what a destination should be."""
    assert _safe_dest(dest) == dest


# ─── rejected, each for its own reason ─────────────────────────────────


def test_protocol_relative_is_rejected():
    """`//host` is THE open redirect: a browser reads it as a host, not a path,
    and it does not look like a URL at a glance. This is the case the whole
    validator exists for."""
    assert _safe_dest("//evil.example/") == CONSOLE_LOGIN_DEFAULT_DEST
    assert _safe_dest("//evil.example") == CONSOLE_LOGIN_DEFAULT_DEST


def test_backslash_variant_is_rejected():
    """Some browsers normalise `/\\host` to `//host`. Same redirect, different
    spelling."""
    assert _safe_dest("/\\evil.example") == CONSOLE_LOGIN_DEFAULT_DEST


@pytest.mark.parametrize(
    "dest",
    [
        "https://evil.example/",
        "http://evil.example/",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
    ],
)
def test_schemes_are_rejected(dest):
    """Anything carrying a scheme leaves this origin — or, for javascript:,
    never was a location at all."""
    assert _safe_dest(dest) == CONSOLE_LOGIN_DEFAULT_DEST


@pytest.mark.parametrize("dest", ["", "   ", "config", "./config", "../etc"])
def test_relative_and_empty_fall_back(dest):
    """A relative path resolves against wherever the browser happens to be, so
    it is not a destination anyone chose."""
    assert _safe_dest(dest) == CONSOLE_LOGIN_DEFAULT_DEST


@pytest.mark.parametrize(
    "dest", ["/config\n/evil", "/config\r\nX", "/con\x00fig", "/config\x7f"]
)
def test_control_characters_are_rejected(dest):
    """No legitimate path contains them, and they are how a JS string context
    gets broken out of."""
    assert _safe_dest(dest) == CONSOLE_LOGIN_DEFAULT_DEST


@pytest.mark.parametrize("dest", [None, 12, {"a": 1}, ["/config"], True])
def test_non_strings_fall_back(dest):
    """A malformed token must not crash the login; it must land on the default."""
    assert _safe_dest(dest) == CONSOLE_LOGIN_DEFAULT_DEST


# ─── the structural guarantee ──────────────────────────────────────────


def test_dest_is_read_from_the_signed_payload_and_not_the_query_string():
    """The load-bearing one.

    The HMAC covers the token, so a destination read from `t` cannot be
    rewritten by whoever holds the link. A `?dest=` parameter would be
    attacker-controlled on an endpoint that plants a session — which is the
    difference between a feature and a vulnerability.

    Asserted against the SOURCE because it is a property of where the value is
    read, and no runtime assertion can show that a query parameter is *not*
    consulted.
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "greenautarky_site"
        / "console_login.py"
    ).read_text()
    assert 'payload.get("dest")' in src, "dest must come from the signed payload"
    for forbidden in (
        'query.get("dest")',
        "query['dest']",
        'query.get("next")',
        'rel_url.query.get("dest")',
    ):
        assert forbidden not in src, (
            f"dest must never be read from the query string ({forbidden})"
        )


def test_the_default_is_the_customer_view():
    """Unchanged behaviour when no destination is asked for: every existing
    caller keeps landing exactly where it did."""
    assert CONSOLE_LOGIN_DEFAULT_DEST == "/"
