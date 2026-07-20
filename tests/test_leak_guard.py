"""Stage B leak-guard (Odoo #516) — filter shapes, scoping gate, wrapping."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from greenautarky_onboarding import leak_guard

# ─── is_user_scoped gate ──────────────────────────────────────────────


def _user(groups=(), is_admin=False, is_owner=False):
    return SimpleNamespace(
        groups=[SimpleNamespace(id=g) for g in groups],
        is_admin=is_admin,
        is_owner=is_owner,
        permissions=MagicMock(),
    )


def test_is_user_scoped_true_only_with_scope_group():
    from greenautarky_onboarding.entity_scope import is_user_scoped
    assert is_user_scoped(_user(groups=["ga_scope_abc"])) is True
    assert is_user_scoped(_user(groups=["system-users"])) is False
    assert is_user_scoped(_user(groups=[])) is False


# ─── _filter_result — one case per shape ──────────────────────────────


def _perm(allowed):
    u = _user(groups=["ga_scope_x"])
    u.permissions.check_entity = lambda eid, pol: eid in allowed
    return u


def test_filter_entity_keyed_map():
    u = _perm({"light.living"})
    res = {"light.living": [1], "light.bedroom": [2]}
    out = leak_guard._filter_result(None, u, "entity_keyed_map", res)
    assert out == {"light.living": [1]}


def test_filter_entity_row_list():
    u = _perm({"light.living"})
    res = [{"entity_id": "light.living"}, {"entity_id": "light.bedroom"}]
    out = leak_guard._filter_result(None, u, "entity_row_list", res)
    assert out == [{"entity_id": "light.living"}]


def test_filter_display_map():
    u = _perm({"light.living"})
    res = {"entities": [{"ei": "light.living"}, {"ei": "light.bedroom"}],
           "categories": {"x": 1}}
    out = leak_guard._filter_result(None, u, "display_map", res)
    assert out["entities"] == [{"ei": "light.living"}]
    assert out["categories"] == {"x": 1}  # untouched


def test_filter_bug_denies_instead_of_leaking():
    u = _perm({"light.living"})
    # a row shape the filter can't understand → a raised error must fall back
    # to empty, never to passing the raw list through
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")
    out = leak_guard._filter_result(None, u, "entity_row_list", [Boom()])
    assert out == []


# ─── install() — idempotent, counts, real registry ────────────────────


async def test_install_wraps_and_is_idempotent(hass):
    from homeassistant.components.websocket_api import const as ws_const
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "config", {})
    await hass.async_block_till_done()

    reg = hass.data[ws_const.DOMAIN]
    before = reg["config/entity_registry/list"][0]
    assert not getattr(before, "_ga_leak_guarded", False)

    n1 = leak_guard.install(hass)
    after = reg["config/entity_registry/list"][0]
    assert getattr(after, "_ga_leak_guarded", False) is True
    assert reg["render_template"][0]._ga_leak_guarded is True

    n2 = leak_guard.install(hass)  # idempotent — no double wrap
    assert reg["config/entity_registry/list"][0] is after
    assert n1 == n2 >= 3


# ─── end-to-end through the wrapper with a fake connection ────────────


class _Conn:
    def __init__(self, user):
        self.user = user
        self.results: list = []
        self.errors: list = []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append((code, message))


def _install_and_get(hass, command):
    from homeassistant.components.websocket_api import const as ws_const
    leak_guard.install(hass)
    return hass.data[ws_const.DOMAIN][command][0]


async def test_scoped_user_render_template_denied(hass):
    from homeassistant.setup import async_setup_component
    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "config", {})
    handler = _install_and_get(hass, "render_template")
    conn = _Conn(_user(groups=["ga_scope_x"]))
    res = handler(hass, conn, {"id": 1, "type": "render_template", "template": "{{ 1 }}"})
    if res is not None:
        await res
    assert conn.errors and conn.errors[0][0]  # ERR_UNAUTHORIZED
    assert not conn.results


async def test_admin_passes_through(hass):
    from homeassistant.setup import async_setup_component
    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "config", {})

    called = {}

    def fake_original(h, c, m):
        called["hit"] = True
        c.send_result(m["id"], "RAW")

    # wrap our fake original directly
    wrapped = leak_guard._wrap(hass, "config/entity_registry/list", fake_original,
                               "entity_row_list")
    conn = _Conn(_user(groups=["system-admin"], is_admin=True))
    res = wrapped(hass, conn, {"id": 1})
    if res is not None:
        await res
    assert called.get("hit") and conn.results == ["RAW"]  # untouched


async def test_scoped_user_result_is_filtered(hass):
    from homeassistant.setup import async_setup_component
    await async_setup_component(hass, "websocket_api", {})
    await async_setup_component(hass, "config", {})

    def fake_original(h, c, m):
        c.send_result(m["id"], [{"entity_id": "light.living"},
                                 {"entity_id": "light.bedroom"}])

    wrapped = leak_guard._wrap(hass, "config/entity_registry/list", fake_original,
                               "entity_row_list")
    u = _perm({"light.living"})
    conn = _Conn(u)
    res = wrapped(hass, conn, {"id": 1})
    if res is not None:
        await res
    assert conn.results == [[{"entity_id": "light.living"}]]
