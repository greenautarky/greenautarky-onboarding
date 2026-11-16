"""Redirects that keep what the caller sent.

A redirect that drops its query string is the most expensive one-line defect
this component has had. It shipped once as the `/` → wizard redirect: the
device label's QR code carries `?pin=…&device=…`, the redirect dropped both,
and every customer who scanned the label still had to type the PIN by hand.
Fixed in 2.7.1 — for that one route.

On 2026-09-22 a sweep for the same shape found it twice more, untouched:

  * ``GET /greenautarky-join``  → ``/greenautarky-setup.html?join=1``
    An invite LINK is the whole point of the sub-user flow, and the frontend
    has read ``?pin=`` from the URL since it was written. The redirect threw it
    away, so only the six digits could ever reach a person.
  * ``GET /greenautarky-setup`` → ``/greenautarky-setup.html``
    The same QR-code case as 2.7.1, one path along.

So the forwarding lives in ONE function that every redirect in this component
calls, and a gate asserts that every redirect target is built through it. A
fix applied per site is a fix that comes back.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web

# A printed label (QR) and an invite link made before 2.13.1 carry the PIN in the
# QUERY STRING: `/?pin=123456&device=...`. The request line itself reaches the
# proxy and its access log before any code of ours runs (the Caddy log filter
# redacts it there); what WE control is that it never travels on from us: not in
# the Location header (Caddy logs response headers), not in our own log. A
# valid PIN is handed to the setup page in a short-lived cookie instead — Caddy
# logs cookies as REDACTED — and the page reads it, submits it in a POST body and
# deletes it. New labels and invite links use the URL FRAGMENT (`#pin=`), which a
# browser never sends at all.
PIN_HINT_COOKIE = "ga_pin_hint"
PIN_HINT_TTL_S = 120
_PIN_RE = re.compile(r"[0-9]{6}")


def set_pin_hint(response: web.StreamResponse, pin: str | None) -> None:
    """Hand a legacy ``?pin=`` to the setup page via cookie, if it is a PIN."""
    if pin and _PIN_RE.fullmatch(pin):
        response.set_cookie(
            PIN_HINT_COOKIE, pin, max_age=PIN_HINT_TTL_S, path="/", samesite="Lax"
        )


def redirect_keeping_query(
    request: web.Request, target: str, *, drop: tuple[str, ...] = (), **extra: str
) -> web.HTTPFound:
    """``HTTPFound`` to *target*, carrying the request's query along.

    The target's own query wins over the incoming one for the same key — a
    route that redirects to ``?join=1`` means it, and a caller must not be able
    to turn that off by sending ``?join=0``. ``extra`` wins over both and is
    where a handler adds something it computed.

    ``pin`` is NEVER forwarded, whatever ``drop`` says (2.13.1): a valid one is
    passed on as a short-lived cookie (see ``set_pin_hint``), so a printed label
    or invite link from before the fragment form still pre-fills the wizard
    without the PIN appearing in a Location header. ``drop`` names further
    incoming keys that must NOT be forwarded.

    Order is preserved and duplicate keys survive, because a query is a list of
    pairs, not a mapping — dropping duplicates silently is how a filter loses
    the second of two values a browser legitimately sent.
    """
    parts = urlsplit(target)
    incoming = parse_qsl(request.query_string, keep_blank_values=True)
    own = parse_qsl(parts.query, keep_blank_values=True)
    fixed = {k for k, _ in own} | set(extra)
    skip = set(drop) | {"pin"}
    merged = [(k, v) for k, v in incoming if k not in fixed and k not in skip] + own
    merged += list(extra.items())
    response = web.HTTPFound(
        urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))
    )
    set_pin_hint(response, next((v for k, v in incoming if k == "pin"), None))
    return response
