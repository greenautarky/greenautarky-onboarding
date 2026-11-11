"""POST /verify_pin must not read the PIN file on the event loop (Odoo #1744).

Core 2026.10 logs "Detected blocking call to open ... pin.py line 253:
stored_pin = pin_path.read_text().strip()" twice per wizard run on K31
(BOSv1.6.0-rc8, 2026-11-10). It works, but a blocking open() in a handler stalls
every other request on the loop, and Core's detector flags it.

The test runs the real view against a real PIN file and spies on the call that
reads it (the spy calls through), recording which thread it ran on.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from greenautarky_site.const import DOMAIN
from greenautarky_site.onboarding import pin as pin_module


class _FakeStore:
    async def async_save(self, data: dict[str, Any]) -> None:
        pass


class _FakeRequest:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self._body = body
        self.cookies: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _setup(hass, monkeypatch, tmp_path, *, with_file=True):
    pin_file = tmp_path / "pin"
    if with_file:
        pin_file.write_text("123456\n")
    monkeypatch.setattr(pin_module, "_pin_file_path", lambda _h: pin_file)
    hass.data[DOMAIN] = {"store": _FakeStore(),
                         "state": {"completed": False, "steps_done": [], "consents": {}}}
    reads: list[bool] = []
    real = Path.read_text
    loop_thread = threading.get_ident()

    def spy(self, *a, **kw):
        if self == pin_file:
            reads.append(threading.get_ident() == loop_thread)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    return reads


async def test_the_pin_file_is_read_in_an_executor_thread(hass, monkeypatch, tmp_path):
    reads = _setup(hass, monkeypatch, tmp_path)
    resp = await pin_module.GAPinVerifyView().post(_FakeRequest(hass, {"pin": "123-456"}))
    assert resp.status == 200
    assert reads, "the PIN file was never read: the test proves nothing"
    assert not any(reads), "PIN file read on the event loop thread"


async def test_a_wrong_pin_is_still_refused(hass, monkeypatch, tmp_path):
    _setup(hass, monkeypatch, tmp_path)
    resp = await pin_module.GAPinVerifyView().post(_FakeRequest(hass, {"pin": "000000"}))
    assert resp.status in (401, 403)


async def test_a_missing_pin_file_is_still_404(hass, monkeypatch, tmp_path):
    _setup(hass, monkeypatch, tmp_path, with_file=False)
    resp = await pin_module.GAPinVerifyView().post(_FakeRequest(hass, {"pin": "123456"}))
    assert resp.status == 404
