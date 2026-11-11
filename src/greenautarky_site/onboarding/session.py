"""Browser-session proof for the wizard (F2, 2.12.13).

Until 2.12.12 a correct sticker PIN set ONE global, persisted flag
(``state["pin_verified"]``) that every later wizard call read, so after one
person verified the PIN every other client could drive the gated steps. Now a
successful PIN check mints a random session id, handed to THAT browser in an
httponly cookie; the server keeps only its sha256 and an expiry, in memory
(never in the persisted state, so the status view cannot expose it). Every
gated step requires a live session. A restart drops all sessions: the resident
enters the PIN again.

Also holds the per-device throttle for the account adopt / replace paths.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from aiohttp import web
from homeassistant.core import HomeAssistant

from ..const import DOMAIN

SESSION_COOKIE = "ga_wizard_session"
SESSION_TTL_S = 30 * 60  # idle timeout, renewed by every valid gated request
SESSION_MAX_LIFETIME_S = 2 * 60 * 60  # hard cap from creation, never renewed

_SESSIONS_KEY = "wizard_sessions"
_THROTTLE_KEY = "wizard_throttle"


def _bucket(hass: HomeAssistant, key: str) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault(key, {})


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _prune(sessions: dict, now: float) -> None:
    # value = (idle expiry, hard expiry)
    for k in [k for k, (idle, hard) in sessions.items() if idle <= now or hard <= now]:
        del sessions[k]


def create_session(hass: HomeAssistant) -> str:
    """Mint a session; returns the secret id (only its hash is kept)."""
    sessions = _bucket(hass, _SESSIONS_KEY)
    now = time.time()
    _prune(sessions, now)
    sid = secrets.token_urlsafe(32)
    sessions[_digest(sid)] = (now + SESSION_TTL_S, now + SESSION_MAX_LIFETIME_S)
    return sid


def session_valid(hass: HomeAssistant, request: web.Request, *, renew: bool = False) -> bool:
    """True iff the request carries a live wizard session cookie.

    ``renew=True`` (the gated steps) slides the idle expiry forward, capped at
    the session's hard lifetime. Read-only callers (status) never renew.
    """
    sid = getattr(request, "cookies", {}).get(SESSION_COOKIE)
    if not sid:
        return False
    sessions = _bucket(hass, _SESSIONS_KEY)
    now = time.time()
    _prune(sessions, now)
    key = _digest(sid)
    if key not in sessions:
        return False
    if renew:
        _idle, hard = sessions[key]
        sessions[key] = (min(now + SESSION_TTL_S, hard), hard)
    return True


def clear_sessions(hass: HomeAssistant) -> None:
    _bucket(hass, _SESSIONS_KEY).clear()


def set_session_cookie(response: web.Response, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=SESSION_MAX_LIFETIME_S,  # the server enforces idle + hard limits
        httponly=True,
        samesite="Strict",
        path="/",
    )


def throttle(hass: HomeAssistant, name: str, *, limit: int, window_s: int) -> web.Response | None:
    """Per-device sliding-window limiter. Counts the call; 429 once over."""
    hits = _bucket(hass, _THROTTLE_KEY).setdefault(name, [])
    now = time.time()
    hits[:] = [t for t in hits if now - t < window_s]
    if len(hits) >= limit:
        retry = int(window_s - (now - hits[0])) + 1
        return web.json_response(
            {"code": "rate_limited", "message": "Too many attempts", "retry_after": retry},
            status=429,
            headers={"Retry-After": str(retry)},
        )
    hits.append(now)
    return None
