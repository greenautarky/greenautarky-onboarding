"""Where the console login lands after it has planted a session.

A module of its own, with NO imports, for one reason: this value ends up in
`window.location.replace(...)` on a page that has just authenticated the
visitor. An unvalidated one is an open redirect on an authenticated endpoint —
hand someone a link, they arrive signed in on a page you chose.

A guard like that has to be testable without standing up Home Assistant, or it
gets tested rarely and reasoned about often. `console_login.py` cannot be
imported without HA; this can, so its tests run anywhere, in under a second, and
there is no excuse not to extend them when a new bypass shape turns up.
"""

from __future__ import annotations

# "/" is the customer's own view — which, on a device whose onboarding is not
# finished, is the onboarding wizard. An operator sent there to reach the admin
# area has no way through, and that is the reason a destination exists at all.
CONSOLE_LOGIN_DEFAULT_DEST = "/"


def safe_dest(raw: object) -> str:
    """A same-origin path, or the default. Never anything else.

    Accepts only a single-slash absolute path. Rejected, each for its own reason:

      "//evil.example"   protocol-relative — a browser reads it as a HOST, so
                         this is the open redirect, and it is the one that does
                         not look like a URL at a glance
      "/\\evil.example"  some browsers normalise this to the above
      "https://x/"       absolute with a scheme
      "javascript:..."   scheme without a slash; would execute
      "config"           relative; resolves against wherever the browser is, so
                         it is not a destination anyone chose
      ""                 empty

    This is the SECOND line of defence. The first is that the destination rides
    inside the HMAC-signed token and never in the query string, so it cannot be
    rewritten in transit. This function covers the case where the signer itself
    is wrong or compromised — which is exactly when a single defence is not one.
    """
    if not isinstance(raw, str) or isinstance(raw, bool):
        return CONSOLE_LOGIN_DEFAULT_DEST
    dest = raw.strip()
    if not dest.startswith("/"):
        return CONSOLE_LOGIN_DEFAULT_DEST
    if dest.startswith("//") or dest.startswith("/\\"):
        return CONSOLE_LOGIN_DEFAULT_DEST
    # Control characters have no place in a path and are how a JS string context
    # gets broken out of.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in dest):
        return CONSOLE_LOGIN_DEFAULT_DEST
    return dest
