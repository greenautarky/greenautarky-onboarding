"""The first-boot onboarding wizard — page, step and completion views.

Onboarding views are unauthenticated (like stock HA onboarding) but gated
by the completion state — once onboarding is done, the endpoints return
403. The actual wizard HTML is built by the frontend build pipeline and
served as a static file by the component root (like onboarding.html).
Includes the admin bypass (``/admin``) and the QA reset endpoint.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from datetime import UTC, datetime
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers.homeassistant import InvalidUser
from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .. import dashboards
from ..consent import async_record_consent
from ..household.dashboards_admin import _reconcile_dashboard_visibility
from ..household.masters import _read_master_user_ids, _write_master_users
from ..household.sub_users import _async_create_linked_person
from ..store import _async_get_hass_provider, _get_state, _get_store
from .pin import _check_pin_verified, _pin_required

_LOGGER = logging.getLogger(__name__)

def _check_not_completed(hass: HomeAssistant) -> web.Response | None:
    """Return a 403 response if onboarding is already completed."""
    state = _get_state(hass)
    if state.get("completed"):
        return web.json_response(
            {"message": "Onboarding already completed"}, status=403
        )
    return None


class GAOnboardingPageView(HomeAssistantView):
    """Redirect to the built greenautarky-setup.html page.

    The actual HTML is built by the frontend build pipeline and served as a
    static file by the frontend component (just like onboarding.html).
    """

    url = "/greenautarky-setup"
    name = "greenautarky_site:page"
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
    name = "greenautarky_site:admin"
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


class GAOnboardingStatusView(HomeAssistantView):
    """Return current onboarding status."""

    url = "/api/greenautarky_site/status"
    name = "api:greenautarky_site:status"
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

    url = "/api/greenautarky_site/gdpr"
    name = "api:greenautarky_site:gdpr"
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
    ``/homeassistant/.storage/greenautarky_site`` to decide whether
    to drive the ring or set it Off.

    Settable any time post-install (no onboarding-completion guard), so
    no ``_check_not_completed`` / ``_check_pin_verified`` gating here.
    """

    url = "/api/greenautarky_site/led"
    name = "api:greenautarky_site:led"
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

    url = "/api/greenautarky_site/telemetry"
    name = "api:greenautarky_site:telemetry"
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

    url = "/api/greenautarky_site/ethernet"
    name = "api:greenautarky_site:ethernet"
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

    url = "/api/greenautarky_site/complete"
    name = "api:greenautarky_site:complete"
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

    url = "/api/greenautarky_site/create_user"
    name = "api:greenautarky_site:create_user"
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

        # Create a linked Person (guaranteed fleet-wide — ADR-0006).
        await _async_create_linked_person(hass, name, user.id)

        # ADR-0006 hybrid main-user flag: the FIRST tenant user created during
        # device onboarding auto-becomes the main user (master) IF no main-user
        # flag exists yet. fleet-manager can override/revoke later (it rewrites
        # the same file). Best-effort — a failure here must not break onboarding.
        try:
            existing = await hass.async_add_executor_job(_read_master_user_ids, hass)
            if not existing:
                await hass.async_add_executor_job(
                    _write_master_users, hass, {user.id}
                )
                _LOGGER.info(
                    "onboarding: auto-flagged first tenant user %s as main user", user.id
                )
        except Exception as err:
            _LOGGER.warning("onboarding: could not auto-flag main user: %s", err)

        # Mark account step as done
        state = _get_state(hass)
        store = _get_store(hass)
        if "account" not in state.get("steps_done", []):
            state.setdefault("steps_done", []).append("account")

        # ADR-0006 matrix: every tenant user gets a personal dashboard,
        # assigned to them in the matrix. Best-effort (never raises); the
        # reconcile turns the assignment into native per-view visibility.
        personal_path = await dashboards.async_create_personal_dashboard(
            hass, state, user.id, name
        )
        await store.async_save(state)
        if personal_path:
            await _reconcile_dashboard_visibility(hass, personal_path, state)

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

    url = "/api/greenautarky_site/reset"
    name = "api:greenautarky_site:reset"
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
        # handler) to avoid a circular import: __init__.py imports this module.
        from .. import (
            PANEL_URL_PATH,
            _async_register_panel,
        )

        if PANEL_URL_PATH not in hass.data.get(DATA_PANELS, {}):
            await _async_register_panel(hass)

        _LOGGER.info("greenautarky onboarding state reset by %s", user.name)
        return self.json({"status": "ok"})
