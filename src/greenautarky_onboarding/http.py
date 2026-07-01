"""HTTP views for greenautarky onboarding.

Onboarding views are unauthenticated (like stock HA onboarding) but gated by
the completion state — once onboarding is done, the endpoints return 403.

Consent views are authenticated and available after onboarding is complete.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers.homeassistant import HassAuthProvider, InvalidUser
from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .consent import async_record_consent, get_outdated_consents
from .const import (
    DOMAIN,
    INVITE_PIN_ALPHABET,
    INVITE_PIN_LENGTH,
    MASTER_USERS_FILE,
    PIN_FILE,
    PIN_FILE_LEGACY,
    PIN_MAX_DELAY,
    SUB_USER_INVITE_DEFAULT_TTL_H,
    SUB_USER_INVITE_MAX_TTL_H,
    SUB_USER_JOIN_MAX_DELAY,
    SUB_USER_MIN_PASSWORD_LEN,
)

_LOGGER = logging.getLogger(__name__)


def _async_get_hass_provider(hass: HomeAssistant) -> HassAuthProvider:
    """Get the Home Assistant auth provider."""
    for prv in hass.auth.auth_providers:
        if prv.type == "homeassistant":
            return prv
    raise RuntimeError("Home Assistant auth provider not found")


# Load HTML templates once at import time
_CONSENT_HTML = (Path(__file__).parent / "consent_page.html").read_text(
    encoding="utf-8"
)
_PW_RESET_HTML = (Path(__file__).parent / "password_reset_page.html").read_text(
    encoding="utf-8"
)
_SUB_USER_JOIN_HTML = (Path(__file__).parent / "sub_user_join_page.html").read_text(
    encoding="utf-8"
)


def _get_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """Get the storage store."""
    return hass.data[DOMAIN]["store"]


def _get_state(hass: HomeAssistant) -> dict[str, Any]:
    """Get the current onboarding state."""
    return hass.data[DOMAIN]["state"]


def _check_not_completed(hass: HomeAssistant) -> web.Response | None:
    """Return a 403 response if onboarding is already completed."""
    state = _get_state(hass)
    if state.get("completed"):
        return web.json_response(
            {"message": "Onboarding already completed"}, status=403
        )
    return None


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


class GAOnboardingPageView(HomeAssistantView):
    """Redirect to the built greenautarky-setup.html page.

    The actual HTML is built by the frontend build pipeline and served as a
    static file by the frontend component (just like onboarding.html).
    """

    url = "/greenautarky-setup"
    name = "greenautarky_onboarding:page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Redirect to the built frontend page."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        if state.get("completed"):
            raise web.HTTPFound("/")
        raise web.HTTPFound("/greenautarky-setup.html")


class GAAdminBypassView(HomeAssistantView):
    """Admin shortcut that bypasses the GA tenant onboarding wizard.

    GET /admin redirects to /auth/authorize with self-referential OAuth
    params and ga_bypass=1, landing the admin on the normal HA login page
    (not the tenant onboarding wizard) regardless of onboarding state.

    Self-referential OAuth params (client_id = device origin, redirect_uri =
    device-origin/config) are required because <ha-authorize> rejects the
    request as "Invalid redirect URI" otherwise. /config is used instead
    of /lovelace so a logged-in admin lands in HA Settings — never on the
    GA setup panel which is the auto-default while onboarding is incomplete.

    A `ga_bypass=1` cookie is also set on the redirect response so the
    server-side IndexView (frontend/__init__.py) skips its own redirect
    when the post-OAuth `/config?code=…` landing arrives without the
    `ga_bypass=1` query (HA's OAuth strips query params from redirect_uri).
    """

    url = "/admin"
    name = "greenautarky_onboarding:admin"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Redirect to /auth/authorize with admin-bypass params."""
        # Build origin from the request so it works regardless of how the
        # device is reached (NetBird IP, LAN IP, hostname, public domain).
        origin = f"{request.scheme}://{request.host}"
        # `auth_callback=1` is required so the HA frontend SPA recognises
        # `/config?code=…&auth_callback=1` as its own OAuth callback and
        # exchanges the code for a token. Without it the SPA discards the
        # code and starts a fresh OAuth round-trip, forcing a second login.
        #
        # `state` mirrors what home-assistant-js-websocket does on its own
        # OAuth init: base64(JSON({hassUrl, clientId})). Without it the SPA
        # crashes with "InvalidCharacterError: atob" while validating the
        # callback. Bytes-form base64 to match what HA produces (no padding
        # is fine — HA decodes via atob which is permissive).
        state = base64.b64encode(
            json.dumps({"hassUrl": origin, "clientId": f"{origin}/"}).encode()
        ).decode()
        params = urlencode(
            {
                "client_id": f"{origin}/",
                "redirect_uri": f"{origin}/config?auth_callback=1",
                "state": state,
                "ga_bypass": "1",
            }
        )
        response = web.Response(
            status=302,
            headers={"location": f"/auth/authorize?{params}"},
        )
        # Mirror IndexView's cookie shape (frontend/__init__.py:735-743).
        response.set_cookie(
            "ga_bypass",
            "1",
            max_age=3600,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return response


# ─── GAConsoleLoginView — signed-token auto-login from fleet-manager ────
#
# Fleet-manager UI offers "🚀 Launch admin console" next to each device.
# The button opens a tab to /api/ga_remote_login on the device, with a
# short-lived HMAC-signed token in the URL. The view:
#   1. validates HMAC against the shared secret (constant-time compare)
#   2. validates expiry + nonce freshness (5-minute window, replay-proof)
#   3. resolves the configured admin user
#   4. issues a refresh_token + access_token via hass.auth
#   5. returns an HTML page that plants `hassTokens` in localStorage
#      and redirects to /
#
# The shared secret is one fleet-wide HMAC key. It lives at
# `/config/.storage/greenautarky_secrets/console_login_secret` (0600,
# root-owned). The `/config/` mount is private to the HA Core container
# — addons mounted on `/share/` cannot read it. ga_manager converge
# writes it on first boot (sourced from fleet-manager's seed).
#
# History: in v1.0.0 the file lived at `/share/ga/console-login-secret`,
# which was readable by every customer-installed addon (HACS-style or
# otherwise) — a real exfil risk. v1.0.1 moves it under `/config/` AND
# adds a migration that copies the secret over on first boot of v1.0.1+,
# then removes the old `/share/` copy. See `_migrate_legacy_console_secret`.
#
# Rotating: write a new secret + restart Core. Replay-protection is
# in-memory only; an HA restart wipes the seen-nonce set, which is fine
# because tokens older than 5 minutes are already rejected by `exp`.
#
# Design rationale:
#   - No password transit. Fleet-manager signs a tiny envelope; the
#     device's HA finds its own admin user.
#   - Replay-window narrow (5 min) so a leaked URL can't be re-used.
#   - HMAC, not asymmetric, because both sides are trusted infra and
#     symmetric is one order of magnitude simpler to operate.
#   - Tokens land in localStorage (not cookie) because that's where
#     HA's frontend reads them (`hassTokens` key, JSON-encoded).
CONSOLE_LOGIN_SECRET_FILE = Path(
    "/config/.storage/greenautarky_secrets/console_login_secret"
)
LEGACY_CONSOLE_LOGIN_SECRET_FILE = Path("/share/ga/console-login-secret")
CONSOLE_LOGIN_NONCE_WINDOW_S = 300
CONSOLE_LOGIN_ACCESS_TOKEN_TTL_S = 1800
# Module-level seen-nonce set (per Core process). Replaced by a real
# cache module if we ever need multi-worker support — Core is currently
# single-worker so a process-local set is sufficient.
_SEEN_NONCES: dict[str, float] = {}


def _migrate_legacy_console_secret() -> bool:
    """Move the console-login secret from the legacy `/share/` path to
    the v1.0.1+ `/config/.storage/greenautarky_secrets/` location.

    Called once at integration setup. Idempotent: a no-op if the legacy
    file isn't there, OR if the new file already exists. Logs but
    doesn't raise on permission errors — the caller will then see no
    secret at the new location and respond with 503, prompting an
    operator to fix the permissions.

    Returns True iff a migration actually happened (= secret moved),
    False otherwise. Useful for tests + an operator audit log line.
    """
    if not LEGACY_CONSOLE_LOGIN_SECRET_FILE.is_file():
        return False
    if CONSOLE_LOGIN_SECRET_FILE.is_file():
        # New path already populated by ga_manager converge or a manual
        # bootstrap — DO NOT overwrite with the legacy value (which may
        # be stale after a rotation that only hit the new path).
        # We do still try to remove the legacy file so it can't be
        # exfiltrated by an addon mounted on /share/.
        try:
            LEGACY_CONSOLE_LOGIN_SECRET_FILE.unlink()
            _LOGGER.info(
                "console-login: removed stale legacy file at %s (new path "
                "already populated)", LEGACY_CONSOLE_LOGIN_SECRET_FILE,
            )
        except OSError as e:
            _LOGGER.warning(
                "console-login: could not remove legacy file %s: %s "
                "(addons mounted on /share/ can still read it)",
                LEGACY_CONSOLE_LOGIN_SECRET_FILE, e,
            )
        return False
    try:
        CONSOLE_LOGIN_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Copy contents (not move) so a permission failure mid-write
        # doesn't leave us with neither file. Then unlink legacy.
        data = LEGACY_CONSOLE_LOGIN_SECRET_FILE.read_text(encoding="utf-8")
        CONSOLE_LOGIN_SECRET_FILE.write_text(data, encoding="utf-8")
        CONSOLE_LOGIN_SECRET_FILE.chmod(0o600)
        LEGACY_CONSOLE_LOGIN_SECRET_FILE.unlink()
        _LOGGER.info(
            "console-login: migrated secret from %s → %s and removed legacy",
            LEGACY_CONSOLE_LOGIN_SECRET_FILE, CONSOLE_LOGIN_SECRET_FILE,
        )
        return True
    except OSError as e:
        _LOGGER.warning(
            "console-login: migration failed (%s): legacy file remains at "
            "%s — operator must move it manually + chmod 0600",
            e, LEGACY_CONSOLE_LOGIN_SECRET_FILE,
        )
        return False


def _read_console_secret() -> bytes | None:
    """Read the shared HMAC secret. Returns None if missing/unreadable."""
    try:
        raw = CONSOLE_LOGIN_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Accept hex or base64url. Both fall back to raw bytes; we don't care
    # about format as long as len >= 32 bytes after decoding attempts.
    for decoder in (bytes.fromhex, lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))):
        try:
            decoded = decoder(raw)
            if len(decoded) >= 32:
                return decoded
        except (ValueError, base64.binascii.Error):
            continue
    # As-is: maybe the file just contains raw key bytes already.
    if len(raw) >= 32:
        return raw.encode("utf-8")
    return None


def _prune_seen_nonces(now_ts: float) -> None:
    """Drop nonces older than the validity window so the dict can't grow
    unbounded under sustained traffic. Caller-driven (no background task)."""
    cutoff = now_ts - CONSOLE_LOGIN_NONCE_WINDOW_S
    stale = [n for n, ts in _SEEN_NONCES.items() if ts < cutoff]
    for n in stale:
        _SEEN_NONCES.pop(n, None)


class GAConsoleLoginView(HomeAssistantView):
    """Signed-token auto-login for the fleet-manager "Launch admin" button.

    GET /api/ga_remote_login?t=<base64url-payload>&s=<base64url-hmac>

    The token payload is a JSON object: `{"nonce": "...", "exp": <epoch_s>}`.
    On success, the response is an HTML page that sets `hassTokens` in
    localStorage and redirects to `/`.
    """

    url = "/api/ga_remote_login"
    name = "greenautarky_onboarding:console_login"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Validate the signed token and issue a browser session."""
        token = request.query.get("t", "")
        sig = request.query.get("s", "")
        if not token or not sig:
            return web.Response(text="Missing 't' or 's' query param.", status=400)

        secret = _read_console_secret()
        if secret is None:
            _LOGGER.warning(
                "console-login: secret file %s missing — fleet-manager has "
                "not pushed it yet (ga_manager converge step issues this).",
                CONSOLE_LOGIN_SECRET_FILE,
            )
            return web.Response(
                text="Console-login secret not provisioned yet.", status=503,
            )

        # Pad base64url for the stdlib decoder.
        try:
            token_bytes = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            sig_bytes = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        except (ValueError, base64.binascii.Error):
            return web.Response(text="Invalid base64 in 't' or 's'.", status=400)

        # Constant-time HMAC check.
        expected = hmac.new(secret, token_bytes, "sha256").digest()
        if not hmac.compare_digest(expected, sig_bytes):
            _LOGGER.warning(
                "console-login: HMAC mismatch from %s (likely stale or forged)",
                request.remote,
            )
            return web.Response(text="Invalid signature.", status=403)

        try:
            payload = json.loads(token_bytes.decode("utf-8"))
            nonce = str(payload["nonce"])
            exp = float(payload["exp"])
        except (ValueError, KeyError) as e:
            return web.Response(text=f"Malformed token: {e}", status=400)

        now_ts = datetime.now(UTC).timestamp()
        if exp < now_ts:
            return web.Response(text="Token expired.", status=403)
        if exp > now_ts + CONSOLE_LOGIN_NONCE_WINDOW_S * 2:
            # Sanity: future exp too far out → reject so a leaked URL can't
            # be replayed for hours. Fleet-manager always signs <= 5 min ahead.
            return web.Response(text="Token expiry too far in the future.", status=400)

        # Replay-protection — single-use nonce inside the validity window.
        _prune_seen_nonces(now_ts)
        if nonce in _SEEN_NONCES:
            _LOGGER.warning(
                "console-login: nonce %s replayed from %s", nonce, request.remote,
            )
            return web.Response(text="Nonce already used.", status=409)
        _SEEN_NONCES[nonce] = now_ts

        hass = request.app[KEY_HASS] if (KEY_HASS := "hass") in request.app else None
        # Newer aiohttp/HA combinations expose the Hass instance via the
        # app key 'hass'; fall back to the request's HomeAssistantView base.
        if hass is None:
            hass = request.app["hass"]

        # Find the admin user. Strategy: configured user_id (DOMAIN data
        # `console_login_user_id`); else the first ACTIVE owner with auth.
        target_user = None
        configured_id = hass.data.get(DOMAIN, {}).get("console_login_user_id")
        if configured_id:
            target_user = await hass.auth.async_get_user(configured_id)
        if target_user is None:
            for u in await hass.auth.async_get_users():
                if u.is_active and u.is_owner:
                    target_user = u
                    break
        if target_user is None:
            _LOGGER.error("console-login: no active owner user found on device")
            return web.Response(text="No admin user to log in as.", status=500)

        # Issue refresh_token + access_token. client_id must look like a URL
        # per HA's RFC8252 validation; the value is informational.
        client_id = "https://fleet-manager.greenautarky.com/"
        try:
            refresh_token = await hass.auth.async_create_refresh_token(
                target_user,
                client_id=client_id,
                access_token_expiration=timedelta(seconds=CONSOLE_LOGIN_ACCESS_TOKEN_TTL_S),
            )
        except ValueError as e:
            # async_create_refresh_token raises on duplicate (user,client_id)
            # pairs — fall back to the existing one.
            _LOGGER.info("console-login: reusing existing refresh_token (%s)", e)
            refresh_token = await hass.auth.async_get_refresh_token_by_token(
                target_user.id  # not a token — won't match; loop instead
            )
            refresh_token = None
            for rt in target_user.refresh_tokens.values():
                if rt.client_id == client_id:
                    refresh_token = rt
                    break
            if refresh_token is None:
                return web.Response(text="Failed to create refresh token.", status=500)

        access_token = hass.auth.async_create_access_token(refresh_token)

        # Build the hassTokens object the HA frontend reads on load.
        origin = f"{request.scheme}://{request.host}"
        ha_tokens = {
            "access_token": access_token,
            "expires": int((now_ts + CONSOLE_LOGIN_ACCESS_TOKEN_TTL_S) * 1000),
            "expires_in": CONSOLE_LOGIN_ACCESS_TOKEN_TTL_S,
            "refresh_token": refresh_token.token,
            "hassUrl": origin,
            "clientId": f"{origin}/",
            "ha_auth_provider": "homeassistant",
        }

        # Inline HTML that plants the tokens and redirects.
        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Launching HA console…</title></head>
<body>
<p>Signing you in to Home Assistant…</p>
<script>
  try {{
    localStorage.setItem('hassTokens', JSON.stringify({json.dumps(ha_tokens)}));
    window.location.replace('/');
  }} catch (e) {{
    document.body.innerText = 'localStorage write failed: ' + e.message;
  }}
</script>
</body>
</html>"""
        _LOGGER.info(
            "console-login: signed in user %s as %s (refresh_token %s)",
            target_user.id, target_user.name, refresh_token.id,
        )
        return web.Response(text=html, content_type="text/html")


class GAOnboardingStatusView(HomeAssistantView):
    """Return current onboarding status."""

    url = "/api/greenautarky_onboarding/status"
    name = "api:greenautarky_onboarding:status"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return onboarding status including PIN verification state."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        response = {**state}

        # Admin bypass cookie: report completed=true so authorize.ts (client-side)
        # skips the GA onboarding redirect and shows the normal HA login.
        if request.cookies.get("ga_bypass") == "1":
            response["completed"] = True

        # Add PIN status fields
        response["pin_required"] = _pin_required(hass)
        response["pin_verified"] = state.get("pin_verified", False)
        locked_until = state.get("pin_locked_until")
        if locked_until:
            remaining = (
                datetime.fromisoformat(locked_until) - datetime.now(UTC)
            ).total_seconds()
            response["pin_retry_after"] = max(0, int(remaining))

        return self.json(response)


class GAOnboardingGDPRView(HomeAssistantView):
    """Handle GDPR consent."""

    url = "/api/greenautarky_onboarding/gdpr"
    name = "api:greenautarky_onboarding:gdpr"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Accept GDPR consent."""
        hass: HomeAssistant = request.app["hass"]
        if err := _check_not_completed(hass):
            return err
        if err := _check_pin_verified(hass):
            return err

        state = _get_state(hass)
        store = _get_store(hass)

        body = await request.json()
        state["gdpr_accepted"] = bool(body.get("accepted", False))
        if "gdpr" not in state["steps_done"]:
            state["steps_done"].append("gdpr")
        await store.async_save(state)

        return self.json({"status": "ok"})


class GALedConfigView(HomeAssistantView):
    """Read/set the iHost status-LED on/off preference.

    The status LED is driven at runtime by ga_manager (Yellow=starting,
    Green=connected, Breathing Red=error). A customer can turn it off;
    this view persists ``led_disabled`` into the onboarding HA Store and
    ga_manager reads it from
    ``/homeassistant/.storage/greenautarky_onboarding`` to decide whether
    to drive the ring or set it Off.

    Settable any time post-install (no onboarding-completion guard), so
    no ``_check_not_completed`` / ``_check_pin_verified`` gating here.
    """

    url = "/api/greenautarky_onboarding/led"
    name = "api:greenautarky_onboarding:led"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the current LED on/off preference."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        return self.json({"led_disabled": bool(state.get("led_disabled", False))})

    async def post(self, request: web.Request) -> web.Response:
        """Set the LED on/off preference."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        store = _get_store(hass)

        body = await request.json()
        state["led_disabled"] = bool(body.get("led_disabled", False))
        state["led_modified"] = datetime.now(UTC).isoformat()
        state["led_modified_by"] = "gaci"
        await store.async_save(state)

        return self.json({"status": "ok", "led_disabled": state["led_disabled"]})


class GAOnboardingTelemetryView(HomeAssistantView):
    """Handle telemetry preferences."""

    url = "/api/greenautarky_onboarding/telemetry"
    name = "api:greenautarky_onboarding:telemetry"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Save telemetry preferences."""
        hass: HomeAssistant = request.app["hass"]
        if err := _check_not_completed(hass):
            return err

        state = _get_state(hass)
        store = _get_store(hass)

        body = await request.json()

        # Forward to greenautarky_telemetry integration
        telemetry_data = hass.data.get("greenautarky_telemetry")
        if telemetry_data:
            prefs = telemetry_data["preferences"]
            prefs["error_logs"] = bool(body.get("error_logs", False))
            prefs["metrics"] = bool(body.get("metrics", False))
            telemetry_store: Store = telemetry_data["store"]
            await telemetry_store.async_save(prefs)

        if "telemetry" not in state["steps_done"]:
            state["steps_done"].append("telemetry")
        await store.async_save(state)

        return self.json({"status": "ok"})


class GAOnboardingEthernetView(HomeAssistantView):
    """Handle Ethernet consent.

    Ethernet is disabled by default (set during provisioning by ga-flasher).
    Users must actively consent to enable it during onboarding.
    """

    url = "/api/greenautarky_onboarding/ethernet"
    name = "api:greenautarky_onboarding:ethernet"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Save Ethernet preference and record consent."""
        hass: HomeAssistant = request.app["hass"]
        if err := _check_not_completed(hass):
            return err

        state = _get_state(hass)
        store = _get_store(hass)

        body = await request.json()
        enable_ethernet = bool(body.get("enable_ethernet", False))

        # Record consent via the version-tracked consent system
        await async_record_consent(hass, store, state, "ethernet")

        if enable_ethernet:

            def _enable_ethernet() -> None:
                subprocess.run(
                    ["ga-manage-ethernet", "enable"],
                    timeout=10,
                    check=False,
                )

            try:
                await hass.async_add_executor_job(_enable_ethernet)
            except Exception:
                _LOGGER.exception("Failed to enable Ethernet")
                return web.json_response(
                    {"message": "Failed to enable Ethernet"}, status=500
                )

        if "ethernet" not in state["steps_done"]:
            state["steps_done"].append("ethernet")
        await store.async_save(state)

        return self.json({"status": "ok"})


class GAOnboardingCompleteView(HomeAssistantView):
    """Mark onboarding as complete."""

    url = "/api/greenautarky_onboarding/complete"
    name = "api:greenautarky_onboarding:complete"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Complete the GA onboarding."""
        hass: HomeAssistant = request.app["hass"]
        if err := _check_not_completed(hass):
            return err

        state = _get_state(hass)
        store = _get_store(hass)

        if "complete" not in state["steps_done"]:
            state["steps_done"].append("complete")
        state["completed"] = True
        await store.async_save(state)

        # Remove the sidebar panel (for app users)
        frontend.async_remove_panel(
            hass, "greenautarky-setup-panel", warn_if_unknown=False
        )

        _LOGGER.info("greenautarky onboarding completed")

        return self.json({"status": "ok", "redirect": "/"})


class GAOnboardingCreateUserView(HomeAssistantView):
    """Create a user account during greenautarky onboarding.

    This endpoint is unauthenticated (the end user has no account yet).
    It creates a normal (non-admin) user and returns an auth_code so the
    frontend can authenticate and continue with authenticated steps.
    """

    url = "/api/greenautarky_onboarding/create_user"
    name = "api:greenautarky_onboarding:create_user"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Create a new user and return auth_code for frontend auth."""
        hass: HomeAssistant = request.app["hass"]
        if err := _check_not_completed(hass):
            return err
        if err := _check_pin_verified(hass):
            return err

        body = await request.json()
        client_id = body.get("client_id", "").strip()
        name = body.get("name", "").strip()
        username = body.get("username", "").strip()
        password = body.get("password", "")
        body.get("language", "de")

        if not name or not username or not password or not client_id:
            return self.json_message(
                "client_id, name, username, and password are required",
                status_code=400,
            )

        # Create user in normal user group (not admin)
        user = await hass.auth.async_create_user(name, group_ids=[GROUP_ID_USER])

        # Create credentials via homeassistant auth provider
        provider = _async_get_hass_provider(hass)
        await provider.async_initialize()
        try:
            await provider.async_add_auth(username, password)
        except InvalidUser:
            return self.json_message("Username already exists", status_code=400)
        credentials = await provider.async_get_or_create_credentials(
            {"username": username}
        )
        await hass.auth.async_link_user(user, credentials)

        # Create person entity if available
        if "person" in hass.config.components:
            from homeassistant.components import person

            await person.async_create_person(hass, name, user_id=user.id)

        # Mark account step as done
        state = _get_state(hass)
        store = _get_store(hass)
        if "account" not in state.get("steps_done", []):
            state.setdefault("steps_done", []).append("account")
            await store.async_save(state)

        # Return auth_code so frontend can authenticate
        from homeassistant.components.auth import create_auth_code

        auth_code = create_auth_code(hass, client_id, credentials)

        _LOGGER.info("Created user via greenautarky onboarding: %s", name)

        return self.json({"auth_code": auth_code})


# ---------------------------------------------------------------------------
# Test/QA reset endpoint (admin-authenticated)
# ---------------------------------------------------------------------------


class GAOnboardingResetView(HomeAssistantView):
    """Reset GA onboarding state to allow re-running the wizard.

    Intended for QA and automated testing (e.g. ga-flasher stage 90).
    Requires admin authentication — the ga-flasher uses the admin token
    obtained during Phase 1 provisioning to call this endpoint.

    Resets: completed, gdpr_accepted, steps_done.
    Preserves: consents (version-tracked separately).
    """

    url = "/api/greenautarky_onboarding/reset"
    name = "api:greenautarky_onboarding:reset"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Reset onboarding state."""
        hass: HomeAssistant = request.app["hass"]

        # Require admin
        user = request["hass_user"]
        if not user.is_admin:
            return web.json_response({"message": "Admin required"}, status=403)

        state = _get_state(hass)
        store = _get_store(hass)

        # Reset wizard state, preserve consents
        state["completed"] = False
        state["gdpr_accepted"] = False
        state["steps_done"] = []
        await store.async_save(state)

        # Re-register the sidebar panel if it was removed on completion.
        # Skip if already registered (e.g. onboarding was never completed).
        from homeassistant.components.frontend import DATA_PANELS

        # Relative import of our own package __init__ — works whether this
        # is loaded as a built-in (homeassistant.components.*) or a
        # custom_component (custom_components.*). Deferred (inside the
        # handler) to avoid a circular import: __init__.py imports http.py.
        from . import (
            PANEL_URL_PATH,
            _async_register_panel,
        )

        if PANEL_URL_PATH not in hass.data.get(DATA_PANELS, {}):
            await _async_register_panel(hass)

        _LOGGER.info("greenautarky onboarding state reset by %s", user.name)
        return self.json({"status": "ok"})


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

    url = "/api/greenautarky_onboarding/verify_pin"
    name = "api:greenautarky_onboarding:verify_pin"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Verify the submitted PIN against the device's PIN file."""
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
    name = "greenautarky_onboarding:password_reset_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the password reset page HTML."""
        return web.Response(text=_PW_RESET_HTML, content_type="text/html")


class GAPasswordResetUsersView(HomeAssistantView):
    """Return list of resettable users after PIN verification.

    Only tenant users (GROUP_ID_USER) are returned — admin accounts
    are excluded and managed via flasher/SSH.
    """

    url = "/api/greenautarky_onboarding/password_reset/users"
    name = "api:greenautarky_onboarding:password_reset:users"
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

    url = "/api/greenautarky_onboarding/password_reset"
    name = "api:greenautarky_onboarding:password_reset"
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


# ---------------------------------------------------------------------------
# Consent views (authenticated — for post-onboarding consent re-confirmation)
# ---------------------------------------------------------------------------


class GAConsentPageView(HomeAssistantView):
    """Serve the standalone consent re-confirmation page."""

    url = "/greenautarky-consent"
    name = "greenautarky_onboarding:consent_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the consent page HTML."""
        return web.Response(text=_CONSENT_HTML, content_type="text/html")


class GAConsentStatusView(HomeAssistantView):
    """Return which consents are outdated."""

    url = "/api/greenautarky_onboarding/consent/status"
    name = "api:greenautarky_onboarding:consent:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return consent status."""
        hass: HomeAssistant = request.app["hass"]
        state = _get_state(hass)
        outdated = get_outdated_consents(state)
        return self.json(
            {
                "consents": state.get("consents", {}),
                "outdated": list(outdated.keys()),
            }
        )


class GAConsentAcceptView(HomeAssistantView):
    """Accept a consent type."""

    url = "/api/greenautarky_onboarding/consent/accept"
    name = "api:greenautarky_onboarding:consent:accept"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Record consent acceptance."""
        hass: HomeAssistant = request.app["hass"]
        store = _get_store(hass)
        state = _get_state(hass)

        body = await request.json()
        consent_type = body.get("type", "")

        if not consent_type:
            return web.json_response({"message": "Missing 'type' field"}, status=400)

        ok = await async_record_consent(hass, store, state, consent_type)
        if not ok:
            return web.json_response(
                {"message": f"Unknown consent type: {consent_type}"}, status=400
            )

        return self.json({"status": "ok"})


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


def _master_users_path(hass: HomeAssistant) -> Path:
    """Path to the master-user allowlist (read-only here; ga_manager writes)."""
    return Path(hass.config.path(MASTER_USERS_FILE))


def _read_master_user_ids(hass: HomeAssistant) -> set[str]:
    """Return the set of HA user-ids flagged as masters.

    Fail CLOSED: a missing or malformed file yields an empty set (no masters),
    so a broken/absent flag can never grant privilege. Format:
    ``{"masters": [{"ha_user_id": "<uuid>"}, ...]}``.
    """
    path = _master_users_path(hass)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.warning("master-users: %s is not valid JSON — treating as empty", path)
        return set()
    ids: set[str] = set()
    for entry in data.get("masters") or []:
        uid = (entry or {}).get("ha_user_id") if isinstance(entry, dict) else None
        if isinstance(uid, str) and uid:
            ids.add(uid)
    return ids


def _is_master(hass: HomeAssistant, user_id: str | None) -> bool:
    """True iff user_id is a flagged master (SYNC — reads a file; use only off
    the event loop, e.g. in tests / executor). Async handlers must use
    ``_async_is_master`` so the flag read doesn't block the loop."""
    return bool(user_id) and user_id in _read_master_user_ids(hass)


async def _async_is_master(hass: HomeAssistant, user_id: str | None) -> bool:
    """Master check with the flag-file read off-loop (executor)."""
    if not user_id:
        return False
    ids = await hass.async_add_executor_job(_read_master_user_ids, hass)
    return user_id in ids


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


class GASubUserInviteView(HomeAssistantView):
    """Master-only: mint a one-time sub-user invite PIN with a TTL.

    Returns the plaintext PIN ONCE (for the master to share); only its hash,
    the issuing master's user-id, and the expiry are stored. The caller must be
    an authenticated user flagged in the master allowlist.
    """

    url = "/api/greenautarky_onboarding/sub_user/invite"
    name = "api:greenautarky_onboarding:sub_user_invite"
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
    """Serve the self-contained sub-user self-registration page.

    Same link family as device setup, but a SEPARATE, REPEATABLE route that
    works AFTER device onboarding is complete (the one-shot wizard is gated on
    ``completed``; this is not). Unauthenticated — the invite PIN is the gate.
    """

    url = "/greenautarky-join"
    name = "greenautarky_onboarding:join_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the join page HTML."""
        return web.Response(text=_SUB_USER_JOIN_HTML, content_type="text/html")


class GASubUserJoinView(HomeAssistantView):
    """Redeem a one-time invite PIN → create a Non-Admin User + linked Person.

    Unauthenticated (the sub-user has no account yet); the invite PIN is the
    gate, backed by an exponential backoff on bad attempts. On success the user
    is auto-linked to the issuing master (parent map) and the invite consumed.
    Mirrors native onboarding: User (GROUP_ID_USER) + linked Person (empty).
    """

    url = "/api/greenautarky_onboarding/sub_user/join"
    name = "api:greenautarky_onboarding:sub_user_join"
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

        if not name or not password or not submitted:
            return self.json_message(
                "name, password and invite_pin are required", status_code=400
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
        # device_trackers → no location; presence stays opt-in).
        if "person" in hass.config.components:
            from homeassistant.components import person

            await person.async_create_person(hass, name, user_id=user.id)

        # Record the parent relationship + consume the one-time invite.
        sub_users = state.get("sub_users", {})
        sub_users[user.id] = {
            "master": master_user_id,
            "created_at": now.isoformat(),
        }
        state["sub_users"] = sub_users
        state["sub_user_invites"] = [i for i in invites if i is not match]
        state["sub_user_join_attempts"] = 0
        state["sub_user_join_locked_until"] = None
        await store.async_save(state)

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

_MASTER_CONSOLE_HTML = (Path(__file__).parent / "master_console_page.html").read_text(
    encoding="utf-8"
)


def _write_master_users(hass: HomeAssistant, ids: set[str]) -> None:
    """Persist the master allowlist to /config/ga/ga-master-users.json."""
    path = _master_users_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"masters": [{"ha_user_id": i} for i in sorted(ids)]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def _require_master(request: web.Request) -> tuple[Any, web.Response | None]:
    """Return (user, None) if the authenticated caller is a master, else
    (None, 403-response). Flag read is off-loop."""
    hass: HomeAssistant = request.app["hass"]
    user = request["hass_user"]
    if not await _async_is_master(hass, getattr(user, "id", None)):
        return None, web.json_response(
            {"message": "Master privileges required"}, status=403
        )
    return user, None


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


async def _reconcile_dashboard_visibility(
    hass: HomeAssistant, url_path: str, state: dict[str, Any]
) -> None:
    """Write per-view ``visible`` on a storage dashboard from the matrix.

    Assigned sub-users PLUS all masters are kept visible (so the master can
    still see/manage the board); everyone else is hidden. An empty assignment
    strips ``visible`` (back to visible-to-all). YAML/missing dashboards are
    skipped. Best-effort — failures log, never raise to the caller.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        return
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return
    dash = data.dashboards.get(url_path)
    if dash is None:
        return
    try:
        config = await dash.async_load(False)
    except Exception:
        _LOGGER.debug("reconcile: cannot load dashboard %s (skipped)", url_path)
        return

    matrix = state.get("sub_user_dashboards") or {}
    assigned = {uid for uid, paths in matrix.items() if url_path in (paths or [])}
    views = config.get("views") or []
    if assigned:
        # Assigned sub-users + all masters keep visibility; everyone else hidden.
        masters = await hass.async_add_executor_job(_read_master_user_ids, hass)
        visible_ids = sorted(assigned | masters)
        for view in views:
            view["visible"] = [{"user": uid} for uid in visible_ids]
    else:
        # No sub-users assigned → strip ``visible`` (back to visible-to-all).
        for view in views:
            view.pop("visible", None)
    config["views"] = views
    try:
        await dash.async_save(config)
    except Exception as err:
        _LOGGER.warning("reconcile: cannot save dashboard %s: %s", url_path, err)


def _available_dashboards(hass: HomeAssistant) -> list[dict[str, str]]:
    """List storage/YAML dashboards (url_path + title) for the matrix UI."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        return []
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return []
    out: list[dict[str, str]] = []
    for url_path, cfg in data.dashboards.items():
        if url_path is None:
            continue  # the default dashboard has no addressable url_path
        title = url_path
        item = getattr(cfg, "config", None)
        if isinstance(item, dict) and item.get("title"):
            title = item["title"]
        out.append({"url_path": url_path, "title": title})
    return out


class GASubUserSetMasterView(HomeAssistantView):
    """Admin-only: add/remove a user in the master allowlist.

    PROTOTYPE / manual provisioning. Production writes this flag via
    ga_manager / ga-fleet-manager; this component normally only reads it.
    """

    url = "/api/greenautarky_onboarding/sub_user/set_master"
    name = "api:greenautarky_onboarding:sub_user_set_master"
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
        if make:
            ids.add(target)
        else:
            ids.discard(target)
        await hass.async_add_executor_job(_write_master_users, hass, ids)
        _LOGGER.info("master allowlist updated by admin %s: %s", user.id, sorted(ids))
        return self.json({"status": "ok", "masters": sorted(ids)})


class GASubUserManageView(HomeAssistantView):
    """Master-only: list the master's sub-users + dashboards + areas.

    Returns everything the management UI needs in one call.
    """

    url = "/api/greenautarky_onboarding/sub_user/list"
    name = "api:greenautarky_onboarding:sub_user_list"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the master's manageable surface."""
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        state = _get_state(hass)
        children = _children_of(state, master.id)
        matrix = state.get("sub_user_dashboards") or {}

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


class GASubUserAssignDashboardView(HomeAssistantView):
    """Master-only: assign/unassign a dashboard to one of the master's
    sub-users (the matrix), then reconcile native per-view visibility."""

    url = "/api/greenautarky_onboarding/sub_user/assign_dashboard"
    name = "api:greenautarky_onboarding:sub_user_assign_dashboard"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Update one matrix cell + reconcile the dashboard."""
        hass: HomeAssistant = request.app["hass"]
        master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        sub_user_id = (body.get("sub_user_id") or "").strip()
        url_path = (body.get("url_path") or "").strip()
        assigned = bool(body.get("assigned", True))
        if not sub_user_id or not url_path:
            return self.json_message(
                "sub_user_id and url_path are required", status_code=400
            )

        state = _get_state(hass)
        store = _get_store(hass)
        if sub_user_id not in _children_of(state, master.id):
            return web.json_response(
                {"message": "Not your sub-user"}, status=403
            )

        matrix = state.setdefault("sub_user_dashboards", {})
        current = set(matrix.get(sub_user_id, []))
        if assigned:
            current.add(url_path)
        else:
            current.discard(url_path)
        matrix[sub_user_id] = sorted(current)
        await store.async_save(state)

        await _reconcile_dashboard_visibility(hass, url_path, state)
        _LOGGER.info(
            "master %s %s dashboard %s for sub-user %s",
            master.id,
            "assigned" if assigned else "unassigned",
            url_path,
            sub_user_id,
        )
        return self.json(
            {"status": "ok", "dashboards": matrix.get(sub_user_id, [])}
        )


class GASubUserRenameAreaView(HomeAssistantView):
    """Master-only: rename a room (area) via the area registry, in-process."""

    url = "/api/greenautarky_onboarding/sub_user/rename_area"
    name = "api:greenautarky_onboarding:sub_user_rename_area"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Rename an area."""
        hass: HomeAssistant = request.app["hass"]
        _master, err = await _require_master(request)
        if err:
            return err

        body = await request.json()
        area_id = (body.get("area_id") or "").strip()
        name = (body.get("name") or "").strip()
        if not area_id or not name:
            return self.json_message(
                "area_id and name are required", status_code=400
            )

        from homeassistant.helpers import area_registry as ar

        reg = ar.async_get(hass)
        if reg.async_get_area(area_id) is None:
            return self.json_message("Unknown area_id", status_code=404)
        try:
            reg.async_update(area_id, name=name)
        except ValueError as e:  # duplicate name, etc.
            return self.json_message(str(e), status_code=400)
        _LOGGER.info("area %s renamed to %r", area_id, name)
        return self.json({"status": "ok", "area_id": area_id, "name": name})


class GAMasterConsolePageView(HomeAssistantView):
    """Serve the prototype Master console page.

    The page itself is harmless static HTML; every API call it makes is
    authenticated (it uses the logged-in master's token from localStorage)
    and master-gated server-side. The production UI is a Lovelace custom card
    (ga-frontend-bundle); this served page is the prototype surface.
    """

    url = "/greenautarky-master"
    name = "greenautarky_onboarding:master_console"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the master console HTML."""
        return web.Response(text=_MASTER_CONSOLE_HTML, content_type="text/html")
