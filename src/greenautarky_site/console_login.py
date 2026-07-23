"""Signed-token operator auto-login from the fleet-manager UI.

Fleet-manager's "Launch admin console" button opens
``/api/ga_remote_login`` with a short-lived HMAC-signed token; the view
validates it and plants a browser session for the device's admin user.
Includes the v1.0.1 migration that moves the shared HMAC secret from the
addon-readable ``/share/`` path to HA Core's private ``/config/.storage``.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

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
    name = "greenautarky_site:console_login"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Validate the signed token and issue a browser session."""
        token = request.query.get("t", "")
        sig = request.query.get("s", "")
        if not token or not sig:
            return web.Response(text="Missing 't' or 's' query param.", status=400)

        # Newer aiohttp/HA combinations expose the Hass instance via the app
        # key 'hass'; fall back to the request's HomeAssistantView base.
        hass = request.app.get("hass") or request.app["hass"]

        # Read the secret off-loop — it's a synchronous file read and HA flags
        # any blocking I/O inside the event loop (`homeassistant.util.loop`).
        secret = await hass.async_add_executor_job(_read_console_secret)
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
