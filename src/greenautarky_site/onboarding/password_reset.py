"""PIN-based password reset for locked-out tenant users.

Unauthenticated but gated by the same sticker PIN as onboarding, with
its own exponential-backoff rate limit. Only tenant users
(GROUP_ID_USER) can be reset — admin accounts are excluded and managed
via flasher/SSH.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.auth.providers.homeassistant import InvalidUser
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from ..const import PIN_MAX_DELAY
from ..store import _async_get_hass_provider, _get_state, _get_store
from .pin import _pin_file_path, _pin_required

_LOGGER = logging.getLogger(__name__)

# Load HTML templates once at import time
_PW_RESET_HTML = (Path(__file__).parent.parent / "password_reset_page.html").read_text(
    encoding="utf-8"
)

# ---------------------------------------------------------------------------
# Password reset (unauthenticated — PIN-based, for locked-out users)
# ---------------------------------------------------------------------------


def _check_pw_reset_rate_limit(
    state: dict[str, Any],
) -> web.Response | None:
    """Check password reset rate limit. Returns 429 response if locked."""
    locked_until = state.get("pw_reset_pin_locked_until")
    if locked_until:
        remaining = (
            datetime.fromisoformat(locked_until) - datetime.now(UTC)
        ).total_seconds()
        if remaining > 0:
            return web.json_response(
                {
                    "status": "locked",
                    "message": "Too many attempts",
                    "retry_after": int(remaining),
                },
                status=429,
            )
    return None


def _verify_pw_reset_pin(
    hass: HomeAssistant,
    state: dict[str, Any],
    submitted_pin: str,
) -> bool:
    """Verify PIN for password reset. Returns True if correct."""
    pin_path = _pin_file_path(hass)
    if not pin_path.exists():
        return False
    stored_pin = pin_path.read_text().strip()
    clean_pin = submitted_pin.strip().replace("-", "")
    return hmac.compare_digest(clean_pin.encode(), stored_pin.encode())


class GAPasswordResetPageView(HomeAssistantView):
    """Serve the standalone password reset page."""

    url = "/greenautarky-password-reset"
    name = "greenautarky_site:password_reset_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the password reset page HTML."""
        return web.Response(text=_PW_RESET_HTML, content_type="text/html")


class GAPasswordResetUsersView(HomeAssistantView):
    """Return list of resettable users after PIN verification.

    Only tenant users (GROUP_ID_USER) are returned — admin accounts
    are excluded and managed via flasher/SSH.
    """

    url = "/api/greenautarky_site/password_reset/users"
    name = "api:greenautarky_site:password_reset:users"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Return resettable users after verifying PIN."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        store = _get_store(hass)

        # Rate limit check
        if err := _check_pw_reset_rate_limit(state):
            return err

        body = await request.json()
        submitted_pin = body.get("pin", "")

        # PIN file must exist
        if not _pin_required(hass):
            return self.json(
                {"error": "No PIN configured on this device"}, status_code=404
            )

        if not _verify_pw_reset_pin(hass, state, submitted_pin):
            # Increment attempts with exponential backoff
            attempts = state.get("pw_reset_pin_attempts", 0) + 1
            state["pw_reset_pin_attempts"] = attempts
            delay = 0
            if attempts >= 2:
                delay = min(5 * (2 ** (attempts - 2)), PIN_MAX_DELAY)
                lock_time = datetime.now(UTC) + timedelta(seconds=delay)
                state["pw_reset_pin_locked_until"] = lock_time.isoformat()
            await store.async_save(state)
            _LOGGER.warning(
                "Invalid password reset PIN attempt %d (next retry in %ds)",
                attempts,
                delay,
            )
            return self.json(
                {
                    "status": "error",
                    "message": "Invalid PIN",
                    "retry_after": delay,
                    "attempts": attempts,
                },
                status_code=401,
            )

        # PIN correct — collect resettable users
        provider = _async_get_hass_provider(hass)
        await provider.async_initialize()

        users = []
        for user in await hass.auth.async_get_users():
            if user.system_generated:
                continue
            if user.is_admin:
                continue
            # Must have homeassistant auth credentials
            username = None
            for cred in user.credentials:
                if cred.auth_provider_type == "homeassistant":
                    username = cred.data.get("username")
                    break
            if username:
                users.append({"name": user.name, "username": username})

        return self.json({"status": "ok", "users": users})


class GAPasswordResetView(HomeAssistantView):
    """Reset a tenant user's password after PIN verification.

    Only GROUP_ID_USER accounts can be reset — admin accounts are
    protected and managed via flasher/SSH.
    """

    url = "/api/greenautarky_site/password_reset"
    name = "api:greenautarky_site:password_reset"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Reset password after PIN verification."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        store = _get_store(hass)

        # Rate limit check
        if err := _check_pw_reset_rate_limit(state):
            return err

        body = await request.json()
        submitted_pin = body.get("pin", "")
        username = body.get("username", "").strip()
        new_password = body.get("new_password", "")

        if not username or not new_password:
            return self.json(
                {"error": "username and new_password are required"},
                status_code=400,
            )

        # PIN file must exist
        if not _pin_required(hass):
            return self.json(
                {"error": "No PIN configured on this device"}, status_code=404
            )

        # Verify PIN
        if not _verify_pw_reset_pin(hass, state, submitted_pin):
            attempts = state.get("pw_reset_pin_attempts", 0) + 1
            state["pw_reset_pin_attempts"] = attempts
            delay = 0
            if attempts >= 2:
                delay = min(5 * (2 ** (attempts - 2)), PIN_MAX_DELAY)
                lock_time = datetime.now(UTC) + timedelta(seconds=delay)
                state["pw_reset_pin_locked_until"] = lock_time.isoformat()
            await store.async_save(state)
            _LOGGER.warning(
                "Invalid password reset PIN attempt %d (next retry in %ds)",
                attempts,
                delay,
            )
            return self.json(
                {
                    "status": "error",
                    "message": "Invalid PIN",
                    "retry_after": delay,
                    "attempts": attempts,
                },
                status_code=401,
            )

        # Verify user is a tenant (GROUP_ID_USER), not admin or system
        target_user = None
        for user in await hass.auth.async_get_users():
            if user.system_generated or user.is_admin:
                continue
            for cred in user.credentials:
                if (
                    cred.auth_provider_type == "homeassistant"
                    and cred.data.get("username") == username
                ):
                    target_user = user
                    break
            if target_user:
                break

        if target_user is None:
            return self.json(
                {"error": "User not found or not resettable"},
                status_code=404,
            )

        # Change password
        provider = _async_get_hass_provider(hass)
        try:
            await provider.async_change_password(username, new_password)
        except InvalidUser:
            return self.json(
                {"error": "User not found in auth provider"},
                status_code=404,
            )

        # Reset PIN attempt counter on success
        state["pw_reset_pin_attempts"] = 0
        state["pw_reset_pin_locked_until"] = None
        await store.async_save(state)

        _LOGGER.info(
            "Password reset via PIN for user '%s' (%s)", username, target_user.name
        )
        return self.json({"status": "ok"})
