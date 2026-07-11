"""Personal-dashboard auto-creation (ADR-0006 matrix).

Covers the dashboards module itself AND its wiring (the
"built-but-never-wired" guard): the onboarding create_user flow, the
sub-user join flow, and the boot re-register + backfill path each get a
reachability test asserting the dashboard REALLY lands in lovelace data +
the frontend panel registry — not just that a helper returned a value.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.components import frontend
from homeassistant.components.lovelace.const import LOVELACE_DATA

from greenautarky_onboarding import dashboards
from greenautarky_onboarding.const import DOMAIN, MASTER_USERS_FILE
from greenautarky_onboarding.http import (
    GAOnboardingCreateUserView,
    GASubUserInviteView,
    GASubUserJoinView,
    async_boot_register_personal_dashboards,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_master_file(hass):
    """The pytest-HA config dir persists across tests AND runs — the
    master-users file must not leak between tests (auto-elect checks it)."""
    path = Path(hass.config.path(MASTER_USERS_FILE))
    if path.exists():
        path.unlink()
    yield
    if path.exists():
        path.unlink()


# --- shared minimal harness (mirrors test_sub_user_join.py) ---------------


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


class _FakeRequest:
    def __init__(self, hass, body=None, hass_user=None) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._items: dict[str, Any] = {}
        if hass_user is not None:
            self._items["hass_user"] = hass_user

    async def json(self) -> dict[str, Any]:
        return self._body

    def __getitem__(self, key: str) -> Any:
        return self._items[key]


def _seed(hass, state: dict[str, Any] | None = None) -> tuple[_FakeStore, dict]:
    store = _FakeStore()
    st = state if state is not None else {
        "completed": True,
        "steps_done": [],
        "consents": {},
    }
    hass.data[DOMAIN] = {"store": store, "state": st}
    return store, st


def _inject_lovelace(hass) -> None:
    """A SimpleNamespace stands in for lovelace's runtime data."""
    hass.data[LOVELACE_DATA] = types.SimpleNamespace(dashboards={})


def _write_master_flag(hass, *user_ids: str) -> None:
    path = Path(hass.config.path(MASTER_USERS_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"masters": [{"ha_user_id": u} for u in user_ids]}),
        encoding="utf-8",
    )


def _body(resp) -> dict[str, Any]:
    return json.loads(resp.body)


async def _ensure_auth_provider(hass) -> None:
    if any(p.type == "homeassistant" for p in hass.auth.auth_providers):
        return
    from homeassistant import auth as ha_auth

    hass.auth = await ha_auth.auth_manager_from_config(
        hass, [{"type": "homeassistant"}], []
    )


async def _make_master(hass):
    """A flagged Non-Admin master — mirrors a real device, where the
    ``admin`` account exists first (HA makes the FIRST user the owner,
    and a master must never be owner/admin)."""
    await _ensure_auth_provider(hass)
    await hass.auth.async_create_user("Geräte-Admin", group_ids=[GROUP_ID_ADMIN])
    master = await hass.auth.async_create_user("Master", group_ids=[GROUP_ID_USER])
    _write_master_flag(hass, master.id)
    return master


async def _join_sub_user(hass, master, name: str = "Anna") -> dict[str, Any]:
    """Issue an invite as master + join as ``name``. Returns the response."""
    resp = await GASubUserInviteView().post(_FakeRequest(hass, {}, hass_user=master))
    assert resp.status == 200, _body(resp)
    pin = _body(resp)["pin"]
    resp = await GASubUserJoinView().post(
        _FakeRequest(
            hass,
            {"invite_pin": pin, "name": name, "password": "sehr-geheim-123",
             "datenschutz_consent": True},
        )
    )
    return resp


# --- unit: the dashboards module itself ------------------------------------


async def test_create_registers_storage_and_panel(hass) -> None:
    """Dashboard lands in lovelace data + frontend panels + state + matrix."""
    _, state = _seed(hass)
    _inject_lovelace(hass)

    url = await dashboards.async_create_personal_dashboard(
        hass, state, "uid-1", "Anna Schmidt"
    )

    assert url == "ga-home-anna_schmidt"
    assert url in hass.data[LOVELACE_DATA].dashboards
    assert url in hass.data[frontend.DATA_PANELS]
    assert state["personal_dashboards"] == {"uid-1": url}
    assert state["sub_user_dashboards"] == {"uid-1": [url]}
    # starter config was seeded
    config = await hass.data[LOVELACE_DATA].dashboards[url].async_load(False)
    assert "Willkommen, Anna Schmidt" in str(config)


async def test_create_is_idempotent(hass) -> None:
    _, state = _seed(hass)
    _inject_lovelace(hass)
    first = await dashboards.async_create_personal_dashboard(hass, state, "u", "X Y")
    second = await dashboards.async_create_personal_dashboard(hass, state, "u", "X Y")
    assert first == second
    assert list(state["personal_dashboards"].values()) == [first]


async def test_url_collision_gets_suffix(hass) -> None:
    _, state = _seed(hass)
    _inject_lovelace(hass)
    frontend.async_register_built_in_panel(
        hass, "lovelace", frontend_url_path="ga-home-anna"
    )
    url = await dashboards.async_create_personal_dashboard(hass, state, "u2", "Anna")
    assert url == "ga-home-anna-2"


async def test_create_without_lovelace_returns_none(hass) -> None:
    """No lovelace set up → best-effort None, state untouched."""
    _, state = _seed(hass)
    url = await dashboards.async_create_personal_dashboard(hass, state, "u", "A B")
    assert url is None
    assert "personal_dashboards" not in state


# --- wiring: sub-user join flow --------------------------------------------


async def test_join_creates_personal_dashboard(hass) -> None:
    store, state = _seed(hass)
    _inject_lovelace(hass)
    master = await _make_master(hass)

    resp = await _join_sub_user(hass, master, "Anna")
    assert resp.status == 200, _body(resp)

    users = await hass.auth.async_get_users()
    anna = next(u for u in users if u.name == "Anna")
    url = state["personal_dashboards"][anna.id]
    assert url in hass.data[LOVELACE_DATA].dashboards
    assert url in hass.data[frontend.DATA_PANELS]
    assert url in state["sub_user_dashboards"][anna.id]
    # persisted (the join saves the store AFTER the dashboard was recorded)
    assert store.saved is not None
    assert store.saved["personal_dashboards"][anna.id] == url
    # reconcile ran: personal views visible to Anna + the master only
    config = await hass.data[LOVELACE_DATA].dashboards[url].async_load(False)
    visible = {v["user"] for v in config["views"][0]["visible"]}
    assert visible == {anna.id, master.id}


async def test_join_survives_dashboard_failure(hass, monkeypatch) -> None:
    """Best-effort isolation: a dashboard error must not break the join."""
    _, state = _seed(hass)
    _inject_lovelace(hass)
    master = await _make_master(hass)

    async def boom(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(dashboards, "_register", boom)
    resp = await _join_sub_user(hass, master, "Bert")
    assert resp.status == 200, _body(resp)
    users = await hass.auth.async_get_users()
    assert any(u.name == "Bert" for u in users)
    assert "personal_dashboards" not in state


# --- wiring: onboarding create_user flow (auto-elected master) -------------


async def test_create_user_creates_master_dashboard(hass, monkeypatch) -> None:
    _, state = _seed(hass, {"completed": False, "steps_done": [], "consents": {}})
    _inject_lovelace(hass)
    await _ensure_auth_provider(hass)
    monkeypatch.setattr(
        "homeassistant.components.auth.create_auth_code",
        lambda hass, client_id, credentials: "test-code",
    )

    resp = await GAOnboardingCreateUserView().post(
        _FakeRequest(
            hass,
            {"client_id": "http://x", "name": "Familie Kern",
             "username": "familie", "password": "sehr-geheim-123"},
        )
    )
    assert resp.status == 200, _body(resp)

    users = await hass.auth.async_get_users()
    kern = next(u for u in users if u.name == "Familie Kern")
    url = state["personal_dashboards"][kern.id]
    assert url in hass.data[LOVELACE_DATA].dashboards
    assert url in hass.data[frontend.DATA_PANELS]
    # the same flow auto-elected them master
    masters = json.loads(
        Path(hass.config.path(MASTER_USERS_FILE)).read_text(encoding="utf-8")
    )
    assert kern.id in {m["ha_user_id"] for m in masters["masters"]}


# --- boot: re-register + backfill ------------------------------------------


async def test_boot_reregisters_without_reseeding(hass) -> None:
    """After a 'restart', the panel comes back and user edits survive."""
    _, state = _seed(hass)
    _inject_lovelace(hass)
    await _ensure_auth_provider(hass)
    user = await hass.auth.async_create_user("Carla", group_ids=[GROUP_ID_USER])
    url = await dashboards.async_create_personal_dashboard(
        hass, state, user.id, "Carla"
    )
    # user edits their dashboard
    edited = {"views": [{"title": "Meins", "cards": []}]}
    await hass.data[LOVELACE_DATA].dashboards[url].async_save(edited)

    # 'restart': runtime registries are empty, state store persists
    hass.data[frontend.DATA_PANELS].clear()
    hass.data[LOVELACE_DATA].dashboards.clear()

    await async_boot_register_personal_dashboards(hass)

    assert url in hass.data[frontend.DATA_PANELS]
    config = await hass.data[LOVELACE_DATA].dashboards[url].async_load(False)
    assert config == edited  # NOT re-seeded


async def test_boot_backfills_master_and_sub_users(hass) -> None:
    """Pre-feature devices self-heal: master + sub-users get dashboards."""
    store, state = _seed(hass)
    _inject_lovelace(hass)
    master = await _make_master(hass)
    await _ensure_auth_provider(hass)
    sub = await hass.auth.async_create_user("Kind", group_ids=[GROUP_ID_USER])
    state["sub_users"] = {sub.id: {"master": master.id}}

    await async_boot_register_personal_dashboards(hass)

    assert master.id in state["personal_dashboards"]
    assert sub.id in state["personal_dashboards"]
    for url in state["personal_dashboards"].values():
        assert url in hass.data[frontend.DATA_PANELS]
    assert store.saved is not None  # persisted


async def test_boot_backfill_skips_admin_and_inactive(hass) -> None:
    _, state = _seed(hass)
    _inject_lovelace(hass)
    await _ensure_auth_provider(hass)
    admin = await hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
    _write_master_flag(hass, admin.id, "no-such-user")

    await async_boot_register_personal_dashboards(hass)

    assert state.get("personal_dashboards", {}) == {}
