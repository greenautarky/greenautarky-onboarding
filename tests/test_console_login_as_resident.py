"""The console login can sign in as THE resident - or refuse. Never guess.

`as: "resident"` in the HMAC-signed token makes the session the one the resident
has. The account is the one the GA onboarding recorded when it created it; zero
or several eligible accounts, a system user, an inactive one, an unknown `as`
value: all refused, and none of them may fall back to the service admin.
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


def _signed(**extra) -> dict:
    body = {"nonce": f"n-{time.monotonic_ns()}", "exp": time.time() + 120, **extra}
    raw = json.dumps(body, separators=(",", ":")).encode()
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return {"t": enc(raw), "s": enc(hmac.new(SECRET, raw, hashlib.sha256).digest())}


class _Req:
    def __init__(self, hass, query) -> None:
        self.app = {"hass": hass}
        self.query = query
        self.remote = "127.0.0.1"
        self.scheme = "http"
        self.host = "device.invalid"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(console_login, "_read_console_secret", lambda: SECRET)
    console_login._SEEN_NONCES.clear()


async def _world(hass, *, marked=("resident",), extra=()):
    """An owner (the service admin), the resident, optional extras."""
    owner = await hass.auth.async_create_user("Service", group_ids=[GROUP_ID_ADMIN])
    users = {"owner": owner}
    for name in ("resident", *extra):
        users[name] = await hass.auth.async_create_user(name, group_ids=[GROUP_ID_USER])
    state = {}
    for name in marked:
        console_as.record_account(state, users[name].id)
    hass.data[DOMAIN] = {"state": state}
    assert owner.is_owner, "fixture: the first user must be the owner"
    return users


def _signed_in(users) -> set[str]:
    return {n for n, u in users.items() if u.refresh_tokens}


@pytest.mark.asyncio
async def test_as_resident_signs_in_the_recorded_resident_not_the_admin(hass):
    users = await _world(hass)
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 200
    assert _signed_in(users) == {"resident"}


@pytest.mark.asyncio
async def test_without_as_it_is_still_the_admin(hass):
    """Must-pass: the old behaviour is untouched."""
    users = await _world(hass)
    resp = await GAConsoleLoginView().get(_Req(hass, _signed()))
    assert resp.status == 200
    assert _signed_in(users) == {"owner"}


@pytest.mark.asyncio
async def test_no_recorded_account_is_refused_and_nobody_is_signed_in(hass):
    users = await _world(hass, marked=())
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 409 and "refused" in resp.text
    assert _signed_in(users) == set()


@pytest.mark.asyncio
async def test_two_recorded_accounts_are_refused_not_guessed(hass):
    users = await _world(hass, marked=("resident", "second"), extra=("second",))
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 409 and "2 onboarding-created" in resp.text
    assert _signed_in(users) == set()


@pytest.mark.asyncio
async def test_an_unrecorded_user_is_never_picked(hass):
    """Another non-admin user exists, but the onboarding did not create it."""
    users = await _world(hass, marked=(), extra=("neighbour",))
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 409
    assert _signed_in(users) == set()


@pytest.mark.asyncio
async def test_a_system_user_is_never_signed_in(hass):
    users = await _world(hass, marked=())
    sysu = await hass.auth.async_create_system_user("svc", group_ids=[GROUP_ID_USER])
    hass.data[DOMAIN]["state"][console_as.ACCOUNT_MARKER_KEY] = [sysu.id]
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 409
    assert _signed_in(users) == set() and not sysu.refresh_tokens


@pytest.mark.asyncio
async def test_an_inactive_account_is_refused(hass):
    users = await _world(hass)
    await hass.auth.async_deactivate_user(users["resident"])
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 409
    assert _signed_in(users) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["admin", "Resident", "", 1, True, ["resident"]])
async def test_an_unknown_as_value_is_refused_not_downgraded(hass, bad):
    users = await _world(hass)
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": bad})))
    assert resp.status == 400
    assert _signed_in(users) == set()


def test_as_is_read_from_the_signed_payload_only():
    """A `?as=` in the query is not looked at: the source has no query read for it."""
    import inspect

    src = inspect.getsource(GAConsoleLoginView.get)
    assert 'query.get("as")' not in src and "query['as']" not in src


# ── the import-free rules ────────────────────────────────────────────────────
def _u(i, active=True, system=False):
    return SimpleNamespace(id=i, is_active=active, system_generated=system)


def test_pick_resident_rules():
    assert console_as.pick_resident(["a"], [_u("a"), _u("b")])[0].id == "a"
    for marked, users in (
        (None, [_u("a")]), ([], [_u("a")]), ("a", [_u("a")]),
        (["a"], []), (["a"], [_u("a", active=False)]), (["a"], [_u("a", system=True)]),
        (["a", "b"], [_u("a"), _u("b")]),
    ):
        user, why = console_as.pick_resident(marked, users)
        assert user is None and why, (marked, users)


def test_record_account_is_idempotent_and_survives_a_bad_value():
    st = {console_as.ACCOUNT_MARKER_KEY: "junk"}
    console_as.record_account(st, "a")
    console_as.record_account(st, "a")
    assert st[console_as.ACCOUNT_MARKER_KEY] == ["a"]


# ── operator session marker (2026-10-30) ───────────────────────────────────
# An admin login from the fleet-manager must never be trapped by the resident's
# onboarding wizard; ga-onboarding-redirect.js skips while this marker is fresh.
# On K31 (fresh flash, wizard open) the "Admin" button landed on the setup page.


@pytest.mark.asyncio
async def test_admin_login_marks_an_operator_session(hass):
    await _world(hass)
    resp = await GAConsoleLoginView().get(_Req(hass, _signed()))
    assert resp.status == 200
    assert "localStorage.setItem('ga_operator_session'" in resp.text
    assert "removeItem('ga_operator_session')" not in resp.text


@pytest.mark.asyncio
async def test_resident_login_clears_the_operator_marker(hass):
    """The test-user view must stay exactly what the resident sees."""
    await _world(hass)
    resp = await GAConsoleLoginView().get(_Req(hass, _signed(**{"as": "resident"})))
    assert resp.status == 200
    assert "localStorage.removeItem('ga_operator_session')" in resp.text
    assert "setItem('ga_operator_session'" not in resp.text


@pytest.mark.asyncio
async def test_marker_is_set_before_the_redirect(hass):
    """Order matters: the redirect script runs on the next page; the marker must exist by then."""
    await _world(hass)
    html = (await GAConsoleLoginView().get(_Req(hass, _signed()))).text
    assert html.index("ga_operator_session") < html.index("window.location.replace")
