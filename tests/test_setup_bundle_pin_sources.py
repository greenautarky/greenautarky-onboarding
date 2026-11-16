"""Where the setup page takes a pre-filled PIN from (Odoo #2159), driven in node.

The setup entry is a compiled bundle patched in place (see BUILD-INFO.txt), so
there is no source to unit-test: the committed bytes are the subject. The block
that reads the address bar is cut out of BOTH builds (latest + es5) and run
against a fake ``window`` / ``document`` / ``history``.

PIN sources, in order: the URL FRAGMENT (`#pin=`, never sent to a server), the
short-lived `ga_pin_hint` cookie (an older label's `?pin=`, converted by the
server), and - join mode only - the query string. The query string of a plain
device-onboarding page is NOT a source (F3c, 2.12.13).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src/greenautarky_site/frontend_bundle"
BLOCK = re.compile(r'try\{const e=new URLSearchParams\(window\.location\.search\),h=.*?\}catch\([et]\)\{\}')

HARNESS = r"""
const block = process.argv[1], scenario = JSON.parse(process.argv[2]);
const calls = [];
global.window = {location: {search: scenario.search, hash: scenario.hash, pathname: "/greenautarky-setup.html"}};
global.document = {cookie: scenario.cookie};
global.history = {replaceState: (a, b, url) => calls.push(url)};
const self = {_joinMode: scenario.join};
new Function(block).call(self);
console.log(JSON.stringify({autoPin: self._autoPin ?? null, device: self._deviceLabel ?? null,
                            cookie: document.cookie, replaced: calls}));
"""


def _bundles() -> list[pathlib.Path]:
    found = sorted(ROOT.glob("frontend_*/greenautarky-setup.*.js"))
    assert len(found) == 2, found  # coverage: latest + es5, fail closed on zero
    return found


def _block(js: pathlib.Path) -> str:
    m = BLOCK.findall(js.read_text(encoding="utf-8"))
    assert len(m) == 1, f"{js.name}: the address-bar block was not found exactly once - " \
                        "the extraction is stale, do not skip this test"
    return m[0]


def _run(js: pathlib.Path, *, search="", hash="", cookie="", join=False) -> dict:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("GA_REQUIRE_BROWSER"):
            pytest.fail("node is required (GA_REQUIRE_BROWSER set)")
        pytest.skip("node not installed")
    out = subprocess.run(
        [node, "-e", HARNESS, _block(js), json.dumps(
            {"search": search, "hash": hash, "cookie": cookie, "join": join})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize("js", _bundles(), ids=lambda p: p.parent.name)
class TestPinSources:
    def test_fragment_prefills_and_is_cleared(self, js):
        r = _run(js, hash="#pin=246813&device=KIB-SON-00000031")
        assert r["autoPin"] == "246813" and r["device"] == "KIB-SON-00000031"
        assert r["replaced"] == ["/greenautarky-setup.html"], "the PIN stayed in the address bar"

    def test_cookie_prefills_and_is_deleted(self, js):
        r = _run(js, search="?device=KIB-SON-1", cookie="a=b; ga_pin_hint=246813; c=d")
        assert r["autoPin"] == "246813"
        assert "Max-Age=0" in r["cookie"], "the hint cookie was not deleted after use"

    def test_query_pin_is_not_a_source_outside_join_mode(self, js):
        r = _run(js, search="?pin=246813&device=KIB-SON-1")
        assert r["autoPin"] is None, "F3c regression: the page reads ?pin= again"
        assert r["device"] == "KIB-SON-1"
        assert r["replaced"] == ["/greenautarky-setup.html"]

    def test_query_pin_still_works_in_join_mode(self, js):
        # An invite link issued before 2.13.1 that reached the page unconverted.
        assert _run(js, search="?join=1&pin=246813", join=True)["autoPin"] == "246813"

    def test_a_malformed_pin_is_ignored(self, js):
        for bad in ("#pin=12345", "#pin=1234567", "#pin=abcdef", "#pin=246813x"):
            assert _run(js, hash=bad)["autoPin"] is None, bad

    def test_nothing_to_read_changes_nothing(self, js):
        r = _run(js)
        assert r["autoPin"] is None and r["replaced"] == []

    def test_the_extraction_can_fail(self, js):
        """Rule 45/51: the harness must be able to go red - a block that does not
        read the fragment would not pass test_fragment_prefills_and_is_cleared."""
        no_fragment = _block(js).replace('h.get("pin")||', "")
        assert no_fragment != _block(js)
