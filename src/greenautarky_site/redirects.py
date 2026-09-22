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

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web


def redirect_keeping_query(
    request: web.Request, target: str, **extra: str
) -> web.HTTPFound:
    """``HTTPFound`` to *target*, carrying the request's query along.

    The target's own query wins over the incoming one for the same key — a
    route that redirects to ``?join=1`` means it, and a caller must not be able
    to turn that off by sending ``?join=0``. ``extra`` wins over both and is
    where a handler adds something it computed.

    Order is preserved and duplicate keys survive, because a query is a list of
    pairs, not a mapping — dropping duplicates silently is how a filter loses
    the second of two values a browser legitimately sent.
    """
    parts = urlsplit(target)
    incoming = parse_qsl(request.query_string, keep_blank_values=True)
    own = parse_qsl(parts.query, keep_blank_values=True)
    fixed = {k for k, _ in own} | set(extra)
    merged = [(k, v) for k, v in incoming if k not in fixed] + own
    merged += list(extra.items())
    return web.HTTPFound(
        urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))
    )
