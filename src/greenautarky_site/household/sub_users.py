"""Sub-user (household) lifecycle — invite, join, manage, remove — ADR-0006.

A Master-User can invite "Sub-Users" who self-register post-completion,
entering only an invite-PIN + password + display name. We mirror native
HA onboarding: create a Non-Admin User AND a linked Person (empty → no
location). The new user is auto-linked to the issuing master (parent map
in the onboarding Store). The invite PIN is one-time, TTL-bounded,
stored hashed; bad join attempts hit an exponential backoff.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers.homeassistant import InvalidUser
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .. import dashboards
from ..const import (
    DATENSCHUTZ_URL,
    INVITE_PIN_ALPHABET,
    INVITE_PIN_LENGTH,
    SUB_USER_CONSENT_VERSION,
    SUB_USER_INVITE_DEFAULT_TTL_H,
    SUB_USER_INVITE_MAX_TTL_H,
    SUB_USER_JOIN_MAX_DELAY,
    SUB_USER_MIN_PASSWORD_LEN,
)
from ..store import _async_get_hass_provider, _get_state, _get_store
from .dashboards_admin import (
    _available_dashboards,
    _reconcile_dashboard_visibility,
)
from .masters import (
    _async_is_master,
    _read_master_user_ids,
    _require_master,
    _write_master_users,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sub-user (household) management — ADR-0006
#
# A "Master-User" (a HA Non-Admin flagged in /config/ga/ga-master-users.json,
# written by ga_manager) can invite "Sub-Users". Sub-users self-register via
# the SAME link, post-completion, entering only an invite-PIN + password +
# display name. We mirror native HA onboarding: create a Non-Admin User AND a
# linked Person (empty → no location). The new user is auto-linked to the
# issuing master (parent map in the onboarding Store).
#
# Security: the master flag + parent map are the server-side boundary. The
# invite PIN is one-time, TTL-bounded, stored hashed; bad join attempts hit an
# exponential backoff. Dashboard assignment + the 4 scoped management ops are a
# later increment (ADR-0006 §Implementation map) — NOT in this foundation.
# ---------------------------------------------------------------------------


def _hash_invite_pin(pin: str) -> str:
    """sha256 hex of an invite PIN (we never store the plaintext)."""
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def _gen_invite_pin() -> str:
    """Cryptographically-random invite PIN from an unambiguous alphabet."""
    return "".join(secrets.choice(INVITE_PIN_ALPHABET) for _ in range(INVITE_PIN_LENGTH))


def _normalize_invite_pin(raw: str) -> str:
    """Normalize user input: upper-case, strip spaces/dashes."""
    return raw.strip().upper().replace("-", "").replace(" ", "")


def _prune_invites(invites: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Drop expired/malformed invites."""
    out: list[dict[str, Any]] = []
    for inv in invites:
        try:
            if datetime.fromisoformat(inv["exp"]) > now:
                out.append(inv)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _slugify_username(name: str) -> str:
    """Derive a base username from a display name (lowercase alnum + '_')."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return base or "user"


async def _async_ensure_person(hass: HomeAssistant) -> bool:
    """Guarantee the ``person`` integration is loaded (ADR-0006 decision:
    a join ALWAYS creates a linked Person, fleet-wide, mirroring native
    onboarding). Some GA OS builds don't pull ``person`` via default_config,
    so load it on demand. Returns True if ``person`` is available afterwards.
    """
    if "person" in hass.config.components:
        return True
    from homeassistant.setup import async_setup_component

    try:
        return bool(await async_setup_component(hass, "person", {}))
    except Exception as err:  # never let this break user creation
        _LOGGER.warning("could not load 'person' integration: %s", err)
        return "person" in hass.config.components


async def _async_create_linked_person(hass: HomeAssistant, name: str, user_id: str) -> None:
    """Create an (empty) Person linked to ``user_id`` — best-effort, guaranteed
    load first. Empty = no device_trackers → no location; presence stays opt-in."""
    if not await _async_ensure_person(hass):
        _LOGGER.warning("person unavailable — sub-user %s created WITHOUT a linked Person", user_id)
        return
    from homeassistant.components import person

    await person.async_create_person(hass, name, user_id=user_id)


async def _async_delete_linked_person(hass: HomeAssistant, user_id: str) -> None:
    """Delete the Person linked to ``user_id`` (best-effort). Uses the person
    storage collection at ``hass.data[person.DOMAIN][1]`` (same handle
    ``async_create_person`` writes through)."""
    try:
        from homeassistant.components import person

        data = hass.data.get(person.DOMAIN)
        if not data or len(data) < 2:
            return
        coll = data[1]
        for item in list(coll.async_items()):
            if item.get("user_id") == user_id:
                await coll.async_delete_item(item["id"])
    except Exception as err:  # dangling person is low-risk; never fail the request
        _LOGGER.warning("could not delete linked person for %s: %s", user_id, err)


class GASubUserInviteView(HomeAssistantView):
    """Master-only: mint a one-time sub-user invite PIN with a TTL.

    Returns the plaintext PIN ONCE (for the master to share); only its hash,
    the issuing master's user-id, and the expiry are stored. The caller must be
    an authenticated user flagged in the master allowlist.
    """

    url = "/api/greenautarky_site/sub_user/invite"
    name = "api:greenautarky_site:sub_user_invite"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Issue an invite for the authenticated master."""
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        if not await _async_is_master(hass, getattr(user, "id", None)):
            return web.json_response(
                {"message": "Master privileges required"}, status=403
            )

        body = await request.json()
        try:
            ttl_h = int(body.get("ttl_hours", SUB_USER_INVITE_DEFAULT_TTL_H))
        except (TypeError, ValueError):
            ttl_h = SUB_USER_INVITE_DEFAULT_TTL_H
        ttl_h = max(1, min(ttl_h, SUB_USER_INVITE_MAX_TTL_H))

        state = _get_state(hass)
        store = _get_store(hass)
        now = datetime.now(UTC)
        invites = _prune_invites(state.get("sub_user_invites", []), now)

        pin = _gen_invite_pin()
        exp = now + timedelta(hours=ttl_h)
        invites.append(
            {
                "pin_sha256": _hash_invite_pin(pin),
                "master_user_id": user.id,
                "exp": exp.isoformat(),
                "created_at": now.isoformat(),
            }
        )
        state["sub_user_invites"] = invites
        await store.async_save(state)

        _LOGGER.info("sub-user invite issued by master %s (ttl %dh)", user.id, ttl_h)
        return self.json(
            {"pin": pin, "expires_at": exp.isoformat(), "ttl_hours": ttl_h}
        )


class GASubUserJoinPageView(HomeAssistantView):
    """Sub-user join entry — reuses the onboarding wizard in "join mode".

    Redirects to the built wizard page with ``?join=1`` so the panel runs the
    minimal invite-PIN → account flow (ADR-0006) with the SAME UI + password
    strength as device onboarding. Repeatable + works after device onboarding is
    complete. Unauthenticated — the invite PIN is the gate.
    """

    url = "/greenautarky-join"
    name = "greenautarky_site:join_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Redirect to the wizard in join mode."""
        raise web.HTTPFound("/greenautarky-setup.html?join=1")


class GASubUserJoinView(HomeAssistantView):
    """Redeem a one-time invite PIN → create a Non-Admin User + linked Person.

    Unauthenticated (the sub-user has no account yet); the invite PIN is the
    gate, backed by an exponential backoff on bad attempts. On success the user
    is auto-linked to the issuing master (parent map) and the invite consumed.
    Mirrors native onboarding: User (GROUP_ID_USER) + linked Person (empty).
    """

    url = "/api/greenautarky_site/sub_user/join"
    name = "api:greenautarky_site:sub_user_join"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Validate the invite and create the sub-user account."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        store = _get_store(hass)
        now = datetime.now(UTC)

        # Global backoff on repeated bad invite attempts.
        locked_until = state.get("sub_user_join_locked_until")
        if locked_until:
            remaining = (
                datetime.fromisoformat(locked_until) - now
            ).total_seconds()
            if remaining > 0:
                return self.json(
                    {
                        "status": "locked",
                        "message": "Too many attempts",
                        "retry_after": int(remaining),
                    },
                    status_code=429,
                )

        body = await request.json()
        name = (body.get("name") or "").strip()
        password = body.get("password") or ""
        submitted = _normalize_invite_pin(body.get("invite_pin") or "")
        client_id = (body.get("client_id") or "").strip()
        # Sub-user data-protection consent (ADR-0006 open point → best-effort).
        # A sub-user is a separate data subject; the owner's device-level GDPR
        # consent does not cover them. The wizard shows a required Datenschutz
        # checkbox in join mode; we also enforce + record it server-side so the
        # UI is never the only gate. Privacy-review-gated (may be extended).
        datenschutz_consent = bool(body.get("datenschutz_consent"))

        if not name or not password or not submitted:
            return self.json_message(
                "name, password and invite_pin are required", status_code=400
            )
        if not datenschutz_consent:
            return self.json_message(
                "Datenschutz consent is required", status_code=400
            )
        if len(password) < SUB_USER_MIN_PASSWORD_LEN:
            return self.json_message(
                f"password too short (min {SUB_USER_MIN_PASSWORD_LEN})",
                status_code=400,
            )

        invites = _prune_invites(state.get("sub_user_invites", []), now)
        submitted_hash = _hash_invite_pin(submitted)
        match: dict[str, Any] | None = None
        for inv in invites:
            if hmac.compare_digest(inv.get("pin_sha256", ""), submitted_hash):
                match = inv
                break

        if match is None:
            # Invalid/expired invite → increment attempts + exponential backoff.
            attempts = state.get("sub_user_join_attempts", 0) + 1
            state["sub_user_join_attempts"] = attempts
            delay = 0
            if attempts >= 2:
                delay = min(5 * (2 ** (attempts - 2)), SUB_USER_JOIN_MAX_DELAY)
                state["sub_user_join_locked_until"] = (
                    now + timedelta(seconds=delay)
                ).isoformat()
            state["sub_user_invites"] = invites  # persist the prune
            await store.async_save(state)
            _LOGGER.warning(
                "sub-user join: invalid invite (attempt %d, next retry %ds)",
                attempts,
                delay,
            )
            return self.json(
                {
                    "status": "error",
                    "message": "Invalid or expired invite",
                    "retry_after": delay,
                },
                status_code=401,
            )

        # Revocation safety: the issuing master must still be authorized.
        master_user_id = match.get("master_user_id")
        if not await _async_is_master(hass, master_user_id):
            state["sub_user_invites"] = [i for i in invites if i is not match]
            await store.async_save(state)
            _LOGGER.warning(
                "sub-user join: issuing master %s no longer authorized",
                master_user_id,
            )
            return self.json_message(
                "Invite issuer is no longer authorized", status_code=403
            )

        # Create the Non-Admin user.
        user = await hass.auth.async_create_user(name, group_ids=[GROUP_ID_USER])

        # Allocate a unique username derived from the display name.
        provider = _async_get_hass_provider(hass)
        await provider.async_initialize()
        base = _slugify_username(name)
        username = base
        suffix = 1
        while True:
            try:
                await provider.async_add_auth(username, password)
                break
            except InvalidUser:
                username = f"{base}{suffix}"
                suffix += 1
                if suffix > 50:
                    await hass.auth.async_remove_user(user)
                    return self.json_message(
                        "Could not allocate a username", status_code=500
                    )
        credentials = await provider.async_get_or_create_credentials(
            {"username": username}
        )
        await hass.auth.async_link_user(user, credentials)

        # Mirror native onboarding: create a linked Person (empty — no
        # device_trackers → no location; presence stays opt-in). ADR-0006
        # decision: guaranteed fleet-wide (load 'person' if the OS didn't).
        await _async_create_linked_person(hass, name, user.id)

        # Record the parent relationship + the sub-user's own consent + consume
        # the one-time invite. The consent record (who = user.id via the key /
        # what-version / when / which policy) is durable in the onboarding Store
        # alongside the master bookkeeping, so the privacy review can later audit
        # or relocate it. Bumping SUB_USER_CONSENT_VERSION triggers re-consent.
        sub_users = state.get("sub_users", {})
        sub_users[user.id] = {
            "master": master_user_id,
            "created_at": now.isoformat(),
            "consent": {
                "datenschutz": {
                    "version": SUB_USER_CONSENT_VERSION,
                    "accepted_at": now.isoformat(),
                    "policy_url": DATENSCHUTZ_URL,
                }
            },
        }
        state["sub_users"] = sub_users
        state["sub_user_invites"] = [i for i in invites if i is not match]
        state["sub_user_join_attempts"] = 0
        state["sub_user_join_locked_until"] = None

        # ADR-0006 matrix: the new sub-user gets a personal dashboard,
        # assigned in the matrix (visible to them + all masters only after
        # the reconcile). Best-effort — never blocks the join.
        personal_path = await dashboards.async_create_personal_dashboard(
            hass, state, user.id, name
        )
        await store.async_save(state)
        if personal_path:
            await _reconcile_dashboard_visibility(hass, personal_path, state)

        _LOGGER.info(
            "sub-user '%s' (user %s) joined under master %s",
            username,
            user.id,
            master_user_id,
        )

        response: dict[str, Any] = {"status": "ok", "username": username}
        if client_id:
            from homeassistant.components.auth import create_auth_code

            response["auth_code"] = create_auth_code(hass, client_id, credentials)
        return self.json(response)


def _username_of(user: Any) -> str | None:
    """Best-effort homeassistant-provider username for a user object."""
    for cred in getattr(user, "credentials", []) or []:
        if cred.auth_provider_type == "homeassistant":
            return cred.data.get("username")
    return None


def _children_of(state: dict[str, Any], master_id: str) -> dict[str, Any]:
    """Sub-users whose parent is master_id."""
    return {
        uid: info
        for uid, info in (state.get("sub_users") or {}).items()
        if (info or {}).get("master") == master_id
    }


async def _disable_orphaned_sub_users(
    hass: HomeAssistant, revoked_master_ids: set[str]
) -> list[str]:
    """DISABLE (never delete) the sub-users of masters that were just revoked.

    ADR-0006 open point (best-effort, provisional): when a master flag is
    removed, its sub-users are orphaned. We ``is_active=False`` them (they keep
    their account, Person, and dashboard assignments — nothing is destroyed) so
    an ex-master's household logins stop, but the privacy review can still decide
    the final fate (keep-disabled / reassign / delete) and a re-flagged master
    re-enables them. Deliberately conservative: disable is reversible, delete is
    not. Returns the sub-user ids that were disabled.

    NOTE: this covers the in-Core ``set_master`` revocation path (prototype /
    manual). In PRODUCTION the flag is revoked by ga-fleet-manager rewriting
    ``/config/ga/ga-master-users.json`` and this component is not notified — that
    path needs its own reconcile hook (fleet-manager stream + privacy review).
    Not wired to component startup on purpose: a transient missing/malformed flag
    file reads as "no masters" (fail-closed) and must NOT mass-disable a whole
    household on a bad read.
    """
    if not revoked_master_ids:
        return []
    state = _get_state(hass)
    disabled: list[str] = []
    for uid, info in list((state.get("sub_users") or {}).items()):
        if (info or {}).get("master") not in revoked_master_ids:
            continue
        user = await hass.auth.async_get_user(uid)
        if user is None or user.is_owner or user.is_admin or not user.is_active:
            continue
        await hass.auth.async_update_user(user, is_active=False)
        disabled.append(uid)
    if disabled:
        _LOGGER.info(
            "disabled %d orphaned sub-user(s) after master revocation: %s",
            len(disabled),
            disabled,
        )
    return disabled


# ---------------------------------------------------------------------------
# Master management plane (PROTOTYPE) — ADR-0006
#
# Scoped operations a Master may perform on their own sub-users, executed
# IN-PROCESS by this component (it has full Core access). HA's Lovelace write
# WS commands are admin-only, so a Non-Admin master cannot do these from the
# browser — the component does them here. Every op re-checks the master flag
# (and, where relevant, the parent relationship) server-side; the UI is a thin
# client. Entity rename is intentionally DEFERRED.
#
# NOTE: `set_master` here is an admin-only convenience for the prototype /
# manual provisioning. In production the master flag is written by
# ga_manager / ga-fleet-manager (config:rw); this component normally only
# READS it. See ADR-0006.
# ---------------------------------------------------------------------------


class GASubUserSetMasterView(HomeAssistantView):
    """Admin-only: add/remove a user in the master allowlist.

    PROTOTYPE / manual provisioning. Production writes this flag via
    ga_manager / ga-fleet-manager; this component normally only reads it.
    """

    url = "/api/greenautarky_site/sub_user/set_master"
    name = "api:greenautarky_site:sub_user_set_master"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Flag (or unflag) a user as master."""
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        if not user.is_admin:
            return web.json_response({"message": "Admin required"}, status=403)

        body = await request.json()
        target = (body.get("user_id") or "").strip()
        make = bool(body.get("master", True))
        if not target:
            return self.json_message("user_id is required", status_code=400)

        ids = await hass.async_add_executor_job(_read_master_user_ids, hass)
        revoked: set[str] = set()
        if make:
            ids.add(target)
        else:
            if target in ids:
                revoked.add(target)
            ids.discard(target)
        await hass.async_add_executor_job(_write_master_users, hass, ids)

        # ADR-0006 open point (best-effort, provisional policy): revoking a
        # master DISABLES its sub-users (reversible), never deletes them.
        disabled = await _disable_orphaned_sub_users(hass, revoked)

        _LOGGER.info("master allowlist updated by admin %s: %s", user.id, sorted(ids))
        return self.json(
            {"status": "ok", "masters": sorted(ids), "disabled_sub_users": disabled}
        )


class GASubUserManageView(HomeAssistantView):
    """Master-only: list the master's sub-users + dashboards + areas.

    Returns everything the management UI needs in one call.
    """

    url = "/api/greenautarky_site/sub_user/list"
    name = "api:greenautarky_site:sub_user_list"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the master's manageable surface."""
        # Local import: scoping/rooms imports this module at module level,
        # so the reverse edge must stay lazy to avoid a cycle.
        from ..scoping import rooms

        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        state = _get_state(hass)
        children = _children_of(state, master.id)
        matrix = state.get("sub_user_dashboards") or {}
        room_matrix = state.get(rooms.STATE_ROOMS) or {}

        users_by_id = {u.id: u for u in await hass.auth.async_get_users()}
        sub_users = []
        for uid in children:
            u = users_by_id.get(uid)
            if u is None:
                continue
            sub_users.append(
                {
                    "user_id": uid,
                    "name": u.name,
                    "username": _username_of(u),
                    "active": u.is_active,
                    # `rooms` drives the generated dashboard. `dashboards` is the
                    # legacy matrix, kept until the last per-user board is gone.
                    "rooms": room_matrix.get(uid, []),
                    "dashboards": matrix.get(uid, []),
                }
            )

        from homeassistant.helpers import area_registry as ar

        areas = [
            {"area_id": a.id, "name": a.name}
            for a in ar.async_get(hass).async_list_areas()
        ]

        return self.json(
            {
                "sub_users": sub_users,
                "dashboards": _available_dashboards(hass),
                "areas": areas,
            }
        )


class GASubUserRemoveView(HomeAssistantView):
    """Master-only: permanently remove one of the master's OWN sub-users.

    ADR-0006 lifecycle decision (2026-07-01): the main user may remove + disable
    their sub-users (scoped self-service). Deletes the Auth user, its linked
    Person, and all matrix/parent bookkeeping, then reconciles any dashboards the
    sub-user was assigned to. Parent relationship is enforced server-side.
    """

    url = "/api/greenautarky_site/sub_user/remove"
    name = "api:greenautarky_site:sub_user_remove"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Delete a sub-user owned by the calling master."""
        # Local import: scoping/rooms imports this module at module level,
        # so the reverse edge must stay lazy to avoid a cycle.
        from ..scoping import rooms

        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        sub_user_id = (body.get("sub_user_id") or "").strip()
        if not sub_user_id:
            return self.json_message("sub_user_id is required", status_code=400)

        state = _get_state(hass)
        store = _get_store(hass)
        if sub_user_id not in _children_of(state, master.id):
            return web.json_response({"message": "Not your sub-user"}, status=403)

        user = await hass.auth.async_get_user(sub_user_id)
        if user is None:
            # Already gone — clean bookkeeping and report success (idempotent).
            (state.get("sub_users") or {}).pop(sub_user_id, None)
            (state.get("sub_user_dashboards") or {}).pop(sub_user_id, None)
            await dashboards.async_delete_personal_dashboard(
                hass, state, sub_user_id
            )
            await store.async_save(state)
            return self.json({"status": "ok", "removed": sub_user_id})

        # Safety: never let a master delete an admin/owner via a spoofed id.
        if user.is_owner or user.is_admin:
            return web.json_response({"message": "Refusing to remove an admin"}, status=403)

        # Dashboards this sub-user was assigned to (reconcile after removal).
        assigned_paths = list((state.get("sub_user_dashboards") or {}).get(sub_user_id, []))

        await _async_delete_linked_person(hass, sub_user_id)
        await hass.auth.async_remove_user(user)

        (state.get("sub_users") or {}).pop(sub_user_id, None)
        (state.get("sub_user_dashboards") or {}).pop(sub_user_id, None)
        # Room grants die with the user — otherwise a future user that happens to
        # reuse this id would inherit his rooms.
        (state.get(rooms.STATE_ROOMS) or {}).pop(sub_user_id, None)
        # The personal dashboard must DIE with the user — an orphaned board
        # plus the visible-strip below made it public to everyone (KB #149 §5a).
        removed_board = await dashboards.async_delete_personal_dashboard(
            hass, state, sub_user_id
        )
        await store.async_save(state)
        for url_path in assigned_paths:
            if url_path == removed_board:
                continue  # deleted, nothing to reconcile
            await _reconcile_dashboard_visibility(hass, url_path, state)

        _LOGGER.info("master %s removed sub-user %s", master.id, sub_user_id)
        return self.json({"status": "ok", "removed": sub_user_id})


class GASubUserSetEnabledView(HomeAssistantView):
    """Master-only: enable/disable login for one of the master's OWN sub-users.

    ADR-0006 lifecycle: a disabled sub-user keeps their account + assignments
    but cannot log in (``is_active=False``). Parent relationship enforced.
    """

    url = "/api/greenautarky_site/sub_user/set_enabled"
    name = "api:greenautarky_site:sub_user_set_enabled"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Toggle a sub-user's active (login-enabled) state."""
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        sub_user_id = (body.get("sub_user_id") or "").strip()
        if "enabled" not in body:
            return self.json_message("enabled is required", status_code=400)
        enabled = bool(body.get("enabled"))
        if not sub_user_id:
            return self.json_message("sub_user_id is required", status_code=400)

        state = _get_state(hass)
        if sub_user_id not in _children_of(state, master.id):
            return web.json_response({"message": "Not your sub-user"}, status=403)

        user = await hass.auth.async_get_user(sub_user_id)
        if user is None:
            return self.json_message("Unknown sub-user", status_code=404)
        if user.is_owner or user.is_admin:
            return web.json_response({"message": "Refusing to modify an admin"}, status=403)

        await hass.auth.async_update_user(user, is_active=enabled)
        _LOGGER.info(
            "master %s %s sub-user %s",
            master.id,
            "enabled" if enabled else "disabled",
            sub_user_id,
        )
        return self.json({"status": "ok", "sub_user_id": sub_user_id, "enabled": enabled})
