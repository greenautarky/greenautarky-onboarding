"""HTTP views for post-onboarding consent re-confirmation.

Consent views are authenticated and available after onboarding is
complete. The version-tracked consent logic itself lives in
:mod:`consent`; these views are the thin HTTP surface over it.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .consent import async_record_consent, get_outdated_consents
from .store import _get_state, _get_store

# Load HTML templates once at import time
_CONSENT_HTML = (Path(__file__).parent / "consent_page.html").read_text(
    encoding="utf-8"
)

# ---------------------------------------------------------------------------
# Consent views (authenticated — for post-onboarding consent re-confirmation)
# ---------------------------------------------------------------------------


class GAConsentPageView(HomeAssistantView):
    """Serve the standalone consent re-confirmation page."""

    url = "/greenautarky-consent"
    name = "greenautarky_site:consent_page"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the consent page HTML."""
        return web.Response(text=_CONSENT_HTML, content_type="text/html")


class GAConsentStatusView(HomeAssistantView):
    """Return which consents are outdated."""

    url = "/api/greenautarky_site/consent/status"
    name = "api:greenautarky_site:consent:status"
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

    url = "/api/greenautarky_site/consent/accept"
    name = "api:greenautarky_site:consent:accept"
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
