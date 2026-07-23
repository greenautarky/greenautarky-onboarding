"""The sticker-PIN gate — proof of physical access to the device.

The PIN file is written to the device during provisioning (ga-flasher
stage 69b) and persists across onboarding resets. Verification is
unauthenticated with exponential backoff against brute force; other
onboarding endpoints call ``_check_pin_verified`` to gate access until
physical access is proven. Includes the v1.0.3 legacy-path migration.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from ..const import PIN_FILE, PIN_FILE_LEGACY, PIN_MAX_DELAY
from ..store import _get_state, _get_store

_LOGGER = logging.getLogger(__name__)

def _pin_file_path(hass: HomeAssistant) -> Path:
    """Get the path to the onboarding PIN file (v1.0.3+ location).

    `.storage/greenautarky_secrets/onboarding_pin` — same filesystem as
    the v1.0.2 path but inside HA Core's private dir. Co-located with
    the console-login secret moved in v1.0.1.
    """
    return Path(hass.config.path(PIN_FILE))


def _legacy_pin_file_path(hass: HomeAssistant) -> Path:
    """Get the path to the v1.0.0..1.0.2 PIN file (= legacy)."""
    return Path(hass.config.path(PIN_FILE_LEGACY))


def _migrate_legacy_pin(hass: HomeAssistant) -> bool:
    """Move the PIN file from the legacy `/config/ga-onboarding-pin` path
    to the v1.0.3+ `/config/.storage/greenautarky_secrets/onboarding_pin`
    location.

    Called once at integration setup. Idempotent: a no-op if the legacy
    file isn't there, OR if the new file already exists. Mirrors the
    console-login secret migration (= _migrate_legacy_console_secret).

    Returns True iff a migration actually happened.
    """
    legacy = _legacy_pin_file_path(hass)
    new = _pin_file_path(hass)
    if not legacy.is_file():
        return False
    if new.is_file():
        # New path already populated (= a fresh v1.0.3 device wrote it
        # directly). Don't overwrite with the legacy value; remove legacy
        # so an addon mounted on /config can't still read the old copy.
        try:
            legacy.unlink()
            _LOGGER.info(
                "onboarding-pin: removed stale legacy file at %s "
                "(new path already populated)", legacy,
            )
        except OSError as e:
            _LOGGER.warning(
                "onboarding-pin: could not remove legacy file %s: %s",
                legacy, e,
            )
        return False
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        data = legacy.read_text(encoding="utf-8")
        new.write_text(data, encoding="utf-8")
        new.chmod(0o600)
        legacy.unlink()
        _LOGGER.info(
            "onboarding-pin: migrated PIN from %s → %s and removed legacy",
            legacy, new,
        )
        return True
    except OSError as e:
        _LOGGER.warning(
            "onboarding-pin: legacy migration failed (%s → %s): %s",
            legacy, new, e,
        )
        return False


def _pin_required(hass: HomeAssistant) -> bool:
    """Check if a PIN file exists on the device."""
    return _pin_file_path(hass).exists()


def _check_pin_verified(hass: HomeAssistant) -> web.Response | None:
    """Return 403 if PIN is required but not yet verified.

    Called by GDPR, create_user, and other endpoints to gate access
    until physical access is proven.
    """
    if not _pin_required(hass):
        return None  # No PIN file — no verification needed
    state = _get_state(hass)
    if state.get("pin_verified"):
        return None  # Already verified
    return web.json_response({"error": "PIN verification required"}, status=403)


# ---------------------------------------------------------------------------
# PIN verification (unauthenticated — proves physical access to device)
# ---------------------------------------------------------------------------


class GAPinVerifyView(HomeAssistantView):
    """Verify the 6-digit onboarding PIN printed on the device sticker.

    The PIN file is written to the device during provisioning (ga-flasher
    stage 69b) and persists across onboarding resets. Exponential backoff
    prevents brute-force attacks from the internet.

    Rate limiting: delay = min(5 * 2^(attempt-2), 3600) for attempt >= 2
    """

    url = "/api/greenautarky_site/verify_pin"
    name = "api:greenautarky_site:verify_pin"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Verify the submitted PIN against the device's PIN file."""
        # Local import: wizard imports this module's gate helpers at module
        # level, so the reverse edge must stay lazy to avoid a cycle.
        from .wizard import _check_not_completed

        hass: HomeAssistant = request.app["hass"]

        if err := _check_not_completed(hass):
            return err

        state = _get_state(hass)
        store = _get_store(hass)

        # Already verified — idempotent
        if state.get("pin_verified"):
            return self.json({"status": "ok"})

        # Check rate limit
        locked_until = state.get("pin_locked_until")
        if locked_until:
            remaining = (
                datetime.fromisoformat(locked_until) - datetime.now(UTC)
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

        # Read PIN from device file
        pin_path = _pin_file_path(hass)
        if not pin_path.exists():
            return self.json(
                {"error": "No PIN configured on this device"}, status_code=404
            )

        stored_pin = pin_path.read_text().strip()

        # Parse submitted PIN (strip dashes, whitespace)
        body = await request.json()
        submitted_pin = body.get("pin", "").strip().replace("-", "")

        # Constant-time comparison to prevent timing attacks
        if hmac.compare_digest(submitted_pin.encode(), stored_pin.encode()):
            # Success
            state["pin_verified"] = True
            if "pin" not in state.get("steps_done", []):
                state.setdefault("steps_done", []).append("pin")
            state["pin_attempts"] = 0
            state["pin_locked_until"] = None
            await store.async_save(state)
            _LOGGER.info("Onboarding PIN verified successfully")
            return self.json({"status": "ok"})

        # Failure — increment attempts with exponential backoff
        attempts = state.get("pin_attempts", 0) + 1
        state["pin_attempts"] = attempts

        delay = 0
        if attempts >= 2:
            delay = min(5 * (2 ** (attempts - 2)), PIN_MAX_DELAY)
            lock_time = datetime.now(UTC) + timedelta(seconds=delay)
            state["pin_locked_until"] = lock_time.isoformat()

        await store.async_save(state)
        _LOGGER.warning("Invalid PIN attempt %d (next retry in %ds)", attempts, delay)
        return self.json(
            {
                "status": "error",
                "message": "Invalid PIN",
                "retry_after": delay,
                "attempts": attempts,
            },
            status_code=401,
        )
