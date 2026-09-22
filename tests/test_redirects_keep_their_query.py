"""Every redirect in this component keeps what the caller sent.

The class, not the instance. It shipped once as the `/` → wizard redirect: the
device label's QR code carries `?pin=&device=`, the redirect dropped both, and
a customer who scanned the label still had to type the PIN. Fixed in 2.7.1 —
for that route only. A sweep on 2026-09-22 found the same shape twice more, in
`/greenautarky-setup` and in `/greenautarky-join`, where it is what stood
between an invite LINK and six digits read out over the phone.

So there are two checks here: the helper behaves, and NO redirect target in the
component is built without it. The second one is what makes this a gate rather
than three fixed lines.
"""

from __future__ import annotations

import pathlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from greenautarky_site.redirects import redirect_keeping_query


class _Req:
    def __init__(self, qs: str) -> None:
        self.query_string = qs


def _loc(exc: web.HTTPFound) -> str:
    return exc.location


@pytest.mark.parametrize(
    "incoming, target, expect_path, expect_query",
    [
        ("pin=123456", "/greenautarky-setup.html?join=1",
         "/greenautarky-setup.html", {"pin": ["123456"], "join": ["1"]}),
        ("pin=123456&device=KIB-SON-00000031", "/greenautarky-setup.html",
         "/greenautarky-setup.html",
         {"pin": ["123456"], "device": ["KIB-SON-00000031"]}),
        ("", "/greenautarky-setup.html?join=1",
         "/greenautarky-setup.html", {"join": ["1"]}),
        # The route's own value wins: a caller must not be able to turn join
        # mode off by sending join=0 to the join page.
        ("join=0&pin=9", "/greenautarky-setup.html?join=1",
         "/greenautarky-setup.html", {"join": ["1"], "pin": ["9"]}),
        ("pin=", "/", "/", {"pin": [""]}),
    ],
)
def test_the_query_survives(incoming, target, expect_path, expect_query):
    exc = redirect_keeping_query(_Req(incoming), target)
    parts = urlsplit(_loc(exc))
    assert parts.path == expect_path
    assert parse_qs(parts.query, keep_blank_values=True) == expect_query


def test_a_handler_can_add_its_own_and_it_wins():
    exc = redirect_keeping_query(_Req("pin=1&x=2"), "/p?join=1", pin="override")
    q = parse_qs(urlsplit(_loc(exc)).query)
    assert q["pin"] == ["override"] and q["join"] == ["1"] and q["x"] == ["2"]


SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "greenautarky_site"
# `raise web.HTTPFound(` / `web.HTTPMovedPermanently(` etc. — anything that
# hands the browser a new location without going through the helper.
_RAW = re.compile(r"web\.HTTP(Found|MovedPermanently|TemporaryRedirect|SeeOther|PermanentRedirect)\(")


def test_no_redirect_bypasses_the_helper():
    """If this fails, someone added a redirect the query cannot survive — put it
    through redirect_keeping_query, or state in the diff why this one must drop
    what the caller sent."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if "frontend_bundle" in path.parts or path.name == "redirects.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _RAW.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "redirect(s) built without redirect_keeping_query:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_itself_can_fail(tmp_path):
    """Rule 45: prove the gate goes red. A scanner that finds nothing because its
    pattern is broken looks exactly like a clean codebase."""
    assert _RAW.search('        raise web.HTTPFound("/greenautarky-setup.html?join=1")')
    assert not _RAW.search('        raise redirect_keeping_query(request, "/x")')
