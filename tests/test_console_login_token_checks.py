"""The console login accepts a correctly signed, current, unused token - and
nothing else.

Through the real view. Every refusal is asserted by its exact status AND by the
outcome: nobody is signed in. The last tests are the must-pass side: a good token
works, also right after refused ones.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER

from greenautarky_site import console_as, console_login
from greenautarky_site.console_login import GAConsoleLoginView
from greenautarky_site.const import DOMAIN

SECRET = b"k" * 32
# Pinned here, not read from the module: the accepted lifetime is part of the contract.
MAX_AHEAD_S = 600


def _enc(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _body(**over) -> dict:
    return {"nonce": f"n-{time.monotonic_ns()}", "exp": time.time() + 120, **over}


def _token(body: dict, *, key: bytes = SECRET) -> dict:
    raw = json.dumps(body, separators=(",", ":")).encode()
    return {"t": _enc(raw), "s": _enc(hmac.new(key, raw, hashlib.sha256).digest())}


async def _call(hass, query):
    req = SimpleNamespace(
        app={"hass": hass}, query=query, remote="127.0.0.1", scheme="http", host="device.invalid"
    )
    return await GAConsoleLoginView().get(req)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(console_login, "_read_console_secret", lambda: SECRET)
    console_login._SEEN_NONCES.clear()
    yield
    console_login._SEEN_NONCES.clear()


@pytest.fixture
async def users(hass):
    owner = await hass.auth.async_create_user("Service", group_ids=[GROUP_ID_ADMIN])
    resident = await hass.auth.async_create_user("resident", group_ids=[GROUP_ID_USER])
    state: dict = {}
    console_as.record_account(state, resident.id)
    hass.data[DOMAIN] = {"state": state}
    assert owner.is_owner
    return [owner, resident]


def _tokens(users) -> int:
    return sum(len(u.refresh_tokens) for u in users)


# ------------------------------------------------------------- signature ---


@pytest.mark.parametrize("as_", [None, "resident"])
async def test_a_token_signed_with_another_key_is_refused(hass, users, as_):
    body = _body(**({"as": as_} if as_ else {}))
    resp = await _call(hass, _token(body, key=b"x" * 32))
    assert resp.status == 403 and "signature" in resp.text.lower()
    assert _tokens(users) == 0


async def test_a_payload_changed_after_signing_is_refused(hass, users):
    body = _body(**{"as": "resident"})
    signed = _token(body)
    changed = _token({k: v for k, v in body.items() if k != "as"})
    resp = await _call(hass, {"t": changed["t"], "s": signed["s"]})
    assert resp.status == 403
    assert _tokens(users) == 0


@pytest.mark.parametrize("cut", [0, 1, 16, 31])
async def test_a_truncated_signature_is_refused(hass, users, cut):
    good = _token(_body())
    digest = base64.urlsafe_b64decode(good["s"] + "=" * (-len(good["s"]) % 4))
    resp = await _call(hass, {"t": good["t"], "s": _enc(digest[:cut])})
    # An empty one is a missing parameter (400), any other length a wrong signature (403).
    assert resp.status == (400 if cut == 0 else 403)
    assert _tokens(users) == 0


@pytest.mark.parametrize("missing", ["t", "s"])
async def test_a_missing_parameter_is_refused(hass, users, missing):
    query = _token(_body())
    del query[missing]
    assert (await _call(hass, query)).status == 400
    assert _tokens(users) == 0


async def test_without_a_provisioned_secret_nothing_is_accepted(hass, users, monkeypatch):
    monkeypatch.setattr(console_login, "_read_console_secret", lambda: None)
    assert (await _call(hass, _token(_body()))).status == 503
    assert (await _call(hass, _token(_body(), key=b""))).status == 503
    assert _tokens(users) == 0


# ---------------------------------------------------------------- expiry ---


@pytest.mark.parametrize("age", [1, 120, 86400])
async def test_an_expired_token_is_refused(hass, users, age):
    resp = await _call(hass, _token(_body(exp=time.time() - age)))
    assert resp.status == 403 and "expired" in resp.text.lower()
    assert _tokens(users) == 0


async def test_a_token_valid_for_too_long_is_refused(hass, users):
    resp = await _call(hass, _token(_body(exp=time.time() + MAX_AHEAD_S + 60)))
    assert resp.status == 400 and "future" in resp.text.lower()
    assert _tokens(users) == 0


@pytest.mark.parametrize("field", ["exp", "nonce"])
async def test_a_token_without_a_required_field_is_refused(hass, users, field):
    body = _body()
    del body[field]
    assert (await _call(hass, _token(body))).status == 400
    assert _tokens(users) == 0


# ---------------------------------------------------------------- replay ---


async def test_a_token_works_exactly_once(hass, users):
    query = _token(_body())
    assert (await _call(hass, query)).status == 200
    before = _tokens(users)
    for _ in range(2):
        assert (await _call(hass, query)).status == 409
    assert _tokens(users) == before


async def test_a_used_nonce_is_refused_in_a_newly_signed_token(hass, users):
    body = _body()
    assert (await _call(hass, _token(body))).status == 200
    before = _tokens(users)
    again = {**body, "exp": body["exp"] + 30, "as": "resident"}
    assert (await _call(hass, _token(again))).status == 409
    assert _tokens(users) == before


# ------------------------------------------------------------- must-pass ---


async def test_a_good_token_signs_in_the_owner(hass, users):
    assert (await _call(hass, _token(_body()))).status == 200
    assert [len(u.refresh_tokens) for u in users] == [1, 0]


async def test_a_refused_token_does_not_use_up_its_nonce(hass, users):
    body = _body()
    assert (await _call(hass, _token(body, key=b"x" * 32))).status == 403
    assert (await _call(hass, _token({**body, "exp": time.time() - 5}))).status == 403
    assert (await _call(hass, _token(body))).status == 200


async def test_a_token_just_inside_the_allowed_lifetime_works(hass, users):
    assert (await _call(hass, _token(_body(exp=time.time() + MAX_AHEAD_S - 30)))).status == 200
