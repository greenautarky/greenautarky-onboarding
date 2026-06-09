"""Integration for greenautarky post-onboarding setup wizard.

Phase 2 onboarding: the built frontend Lit panel is served at
/greenautarky-setup.html (like stock onboarding.html). A sidebar panel
is also registered so the wizard is accessible from the mobile app.
Handles user account creation, GDPR consent, and analytics preferences.

After onboarding, manages consent re-confirmation via HA repairs system.

This is the custom_component form of the integration. It used to live as a
built-in component in our HA Core fork (greenautarky/ha-core branch
ga/custom-onboarding); migrated 2026-05-XX to decouple from HA Core
lifecycle. See ga-ihost-docs/MIGRATION-CUSTOM-COMPONENT.md.

Two changes vs. the built-in version that lived in the fork:
1. Frontend assets (HTML + JS bundles) are bundled inside this component
   and registered via `async_register_static_paths` — they used to be
   shipped via the GA-customized home-assistant-frontend PyPI package.
2. The `/` → `/greenautarky-setup.html` redirect was a patch to
   `frontend/__init__.py` `IndexView.get()` in the fork; it is now a
   client-side JS module injected via `frontend.add_extra_js_url`.
   (The aiohttp-middleware approach tried in v0.1.x does not work from
   a custom_component — the middleware list is frozen by setup time;
   see ga-ihost-docs/MIGRATION-CUSTOM-COMPONENT-FINDINGS.md Finding 20.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .consent import async_check_and_create_issues
from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .http import (
    GAAdminBypassView,
    GAConsentAcceptView,
    GAConsentPageView,
    GAConsentStatusView,
    GAConsoleLoginView,
    GAOnboardingCompleteView,
    GAOnboardingCreateUserView,
    GAOnboardingEthernetView,
    GAOnboardingGDPRView,
    GAOnboardingPageView,
    GAOnboardingResetView,
    GAOnboardingStatusView,
    GAOnboardingTelemetryView,
    GAPasswordResetPageView,
    GAPasswordResetUsersView,
    GAPasswordResetView,
    GAPinVerifyView,
    _get_state,
    _migrate_legacy_console_secret,
    _migrate_legacy_pin,
)

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/greenautarky_onboarding_static"
PANEL_URL_PATH = "greenautarky-setup-panel"

# URL of the client-side `/` → wizard redirect JS module (Finding 20 fix).
REDIRECT_JS_URL = "/greenautarky_onboarding_redirect.js"

DEFAULT_STATE: dict[str, Any] = {
    "completed": False,
    "gdpr_accepted": False,
    "steps_done": [],
    "consents": {},
}


def _migrate_v1_to_v2(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate storage from v1 to v2: add consents dict.

    If GDPR was already accepted during onboarding, seed consents.gdpr
    with version 1 so the user isn't immediately prompted to re-confirm.
    """
    if "consents" not in state:
        state["consents"] = {}
    if state.get("gdpr_accepted") and "gdpr" not in state["consents"]:
        state["consents"]["gdpr"] = {
            "version": 1,
            "accepted_at": "migrated-from-v1",
        }
    return state


class _MigratableStore(Store):
    """A Store subclass that knows how to migrate v1 → v2 state.

    Required because HA's base Store raises NotImplementedError from
    `_async_migrate_func` if a stored entry has a version OLDER than
    the one we pass to `Store(hass, STORAGE_VERSION, ...)`. Without this
    override, ANY v1 state file on disk would crash setup outright —
    which is exactly how K31 BOSv1.2.6 bench testing caught the gap.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        if old_major_version < 2:
            return _migrate_v1_to_v2(old_data)
        return old_data


async def _async_setup_common(hass: HomeAssistant) -> bool:
    """Shared setup logic — called from both async_setup (yaml) and async_setup_entry (config_flow).

    Idempotent: re-entry detected via DOMAIN-in-hass.data.
    """
    if DOMAIN in hass.data:
        return True

    # Use the migration-aware Store subclass — without it any state file
    # written by a prior version (v1) crashes setup with NotImplementedError
    # on the base _async_migrate_func. See _MigratableStore docstring.
    store: Store[dict[str, Any]] = _MigratableStore(hass, STORAGE_VERSION, STORAGE_KEY)
    state = await store.async_load()

    # `loaded_from_storage` distinguishes a real GA-provisioned device from
    # an old / pre-existing one. See the `state is None` branch below.
    loaded_from_storage = state is not None

    if state is None:
        # No stored onboarding state.
        #
        # A factory-provisioned GA device ALWAYS has this storage entry —
        # provisioning stage 91 writes it (completed=false, tenant_mode=true)
        # so the wizard runs on first boot.
        #
        # Its ABSENCE therefore means this is NOT a freshly GA-provisioned
        # device: it is an OLD device that was set up before GA onboarding
        # existed (or any device the installer chose not to trigger). Such a
        # customer is already up and running — they must NOT be dragged into
        # the onboarding wizard. Default to completed=true: no wizard, no
        # `/` redirect, no sidebar panel. The API views are still registered
        # (harmless) so a later explicit trigger can still start the wizard.
        #
        # To deliberately run the wizard on such a device, write the
        # storage entry explicitly (installer SET_TRIGGER=always, or the
        # factory pipeline). Presence of the storage entry is the wizard
        # opt-in signal.
        state = {
            "completed": True,
            "tenant_mode": False,
            "gdpr_accepted": False,
            "steps_done": [],
            "consents": {},
        }
        _LOGGER.info(
            "No greenautarky onboarding state stored — treating device as "
            "already onboarded (no wizard). This is expected on devices "
            "field-upgraded from before GA onboarding existed."
        )
    elif "consents" not in state:
        state = _migrate_v1_to_v2(state)
        await store.async_save(state)

    hass.data[DOMAIN] = {"store": store, "state": state}

    # Register onboarding HTTP views (always — status check needs to work)
    hass.http.register_view(GAOnboardingPageView())
    hass.http.register_view(GAAdminBypassView())
    hass.http.register_view(GAOnboardingStatusView())
    hass.http.register_view(GAOnboardingGDPRView())
    hass.http.register_view(GAOnboardingTelemetryView())
    hass.http.register_view(GAOnboardingEthernetView())
    hass.http.register_view(GAOnboardingCompleteView())
    hass.http.register_view(GAOnboardingCreateUserView())
    hass.http.register_view(GAOnboardingResetView())
    hass.http.register_view(GAPinVerifyView())

    # v1.0.0 shipped the console-login HMAC secret at `/share/ga/…` —
    # addon-readable, an exfil risk. v1.0.1+ keeps it under `/config/` and
    # migrates the old file on first boot. Best-effort: failures log a
    # warning, do NOT block setup (operator can fix perms; the view will
    # respond 503 until the secret is in the new location).
    await hass.async_add_executor_job(_migrate_legacy_console_secret)

    # v1.0.0..1.0.2 stored the onboarding PIN at `/config/ga-onboarding-pin`
    # — readable by any addon that maps `[config:rw]`. v1.0.3+ moves it
    # under `.storage/greenautarky_secrets/onboarding_pin` (= HA Core
    # convention for private files). Same migration shape as the console
    # secret above; same best-effort policy.
    await hass.async_add_executor_job(_migrate_legacy_pin, hass)

    # Signed-token auto-login from the fleet-manager UI's "Launch admin
    # console" button. See GAConsoleLoginView for the token contract.
    hass.http.register_view(GAConsoleLoginView())

    # Password reset views (unauthenticated, PIN-gated)
    hass.http.register_view(GAPasswordResetPageView())
    hass.http.register_view(GAPasswordResetUsersView())
    hass.http.register_view(GAPasswordResetView())

    # Consent HTTP views (authenticated, always available)
    hass.http.register_view(GAConsentPageView())
    hass.http.register_view(GAConsentStatusView())
    hass.http.register_view(GAConsentAcceptView())

    # Bundled frontend assets — used to be shipped via the GA-customized
    # home-assistant-frontend PyPI package. Now bundled with this component
    # so we don't need a forked frontend at all.
    await _async_register_frontend_bundle(hass)

    # Hide HA's stock sidebar panels that the GA tenant flow doesn't use.
    # Keeps the sidebar focused on the GA-relevant surface (Übersicht +
    # the addons + settings only). The panels are still defined in HA Core
    # — they just don't appear in the sidebar (and the routes 404 from
    # the operator's perspective). Reversible without a redeploy: set
    # `greenautarky_onboarding: hide_default_panels: false` (or unset) in
    # configuration.yaml + restart Core to bring them back.
    #
    # Why here, not via a Lovelace strategy: panel visibility is a
    # frontend-config concern that exists outside of dashboard rendering.
    # frontend.async_remove_panel is the canonical HA API.
    _hide_default_ha_panels(hass)

    # Sidebar panel (mobile app), only shown while onboarding incomplete
    if not state.get("completed"):
        await _async_register_panel(hass)
        _LOGGER.info("greenautarky onboarding available at /greenautarky-setup")

    # Install the `/` → wizard redirect as a client-side JS module
    # injected into the HA frontend (replaces the aiohttp middleware
    # approach, which cannot work from a custom_component — Finding 20).
    if not state.get("completed"):
        _register_redirect_js(hass)
        # ALSO install a server-side patch on IndexView.get so the redirect
        # works for the FIRST visit, before any HA JS bundle loads. Without
        # this the customer sees the HA stock onboarding/login on a fresh
        # device because add_extra_js_url only injects into the
        # authenticated dashboard HTML (never into /onboarding.html nor
        # /auth/authorize) — confirmed in HA Core 2025.11.x source.
        _patch_index_view_for_wizard_redirect(hass)

    # Check for outdated consents and create repair issues if needed.
    # Only for devices with a REAL stored onboarding state — an old device
    # with no stored state never gave GA consent, so consent-version checks
    # would just produce spurious "please re-confirm" repair issues.
    if loaded_from_storage and state.get("completed"):
        async_check_and_create_issues(hass, state)

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Yaml-style setup (legacy / fallback path).

    Lets `greenautarky_onboarding:` in configuration.yaml still work.
    Modern install path uses config_entry → async_setup_entry.
    """
    return await _async_setup_common(hass)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-entry setup (Variant B — triggered by storage entry on restart).

    Installer or HAOS overlay writes a config_entry to
    `.storage/core.config_entries`; HA Core then calls this on next start.
    """
    return await _async_setup_common(hass)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Clean up on unload.

    NOTE: HA Core does not currently support removing registered HTTP views
    or middlewares at runtime. Unloading + reloading the integration
    in a single HA Core process will leak duplicate handlers. A full HA
    Core restart is needed for a clean unload. This is acceptable because
    the integration is install-once / live-forever (customer onboarding
    state is permanent).
    """
    hass.data.pop(DOMAIN, None)
    return True


async def _async_register_frontend_bundle(hass: HomeAssistant) -> None:
    """Register the bundled HTML + JS assets as HA static paths.

    Originally these were served by the GA-customized home-assistant-frontend
    PyPI package; now bundled inside this component. URL paths match the
    upstream registrations (/greenautarky-setup.html, /frontend_latest/...,
    /frontend_es5/...) so the HTML's hardcoded asset references continue to
    work.

    NOTE: HA's `frontend` component also registers `/frontend_latest` and
    `/frontend_es5` as broad static directories. Our specific-file
    registrations take precedence for the greenautarky bundles. If we
    later see asset 404s, this would be the first place to look.
    """
    # The filesystem scan runs in an executor — iterdir()/exists() are
    # blocking calls and must not run inside the event loop (Finding 21).
    configs = await hass.async_add_executor_job(_scan_frontend_bundle)
    if configs is None:
        _LOGGER.error(
            "frontend_bundle/ missing — wizard HTML will 404. "
            "Reinstall the custom_component."
        )
        return

    await hass.http.async_register_static_paths(configs)
    _LOGGER.debug("Registered %d static paths for the wizard bundle", len(configs))


def _scan_frontend_bundle() -> list[StaticPathConfig] | None:
    """Synchronously scan frontend_bundle/ and build StaticPathConfig list.

    Runs in an executor (filesystem I/O). Returns None if the bundle dir
    is missing.
    """
    bundle_dir = Path(__file__).parent / "frontend_bundle"
    if not bundle_dir.exists():
        return None

    configs = [
        StaticPathConfig(
            "/greenautarky-setup.html",
            str(bundle_dir / "greenautarky-setup.html"),
            True,
        ),
        # The `/` → wizard redirect JS module (injected via add_extra_js_url).
        StaticPathConfig(
            REDIRECT_JS_URL,
            str(bundle_dir / "ga-onboarding-redirect.js"),
            True,
        ),
    ]
    # Hashed JS bundles — register each file individually under the upstream URLs.
    for sub in ("frontend_latest", "frontend_es5"):
        sub_dir = bundle_dir / sub
        if not sub_dir.exists():
            continue
        for file in sub_dir.iterdir():
            if file.is_file() and file.name.startswith("greenautarky-setup"):
                configs.append(
                    StaticPathConfig(f"/{sub}/{file.name}", str(file), True)
                )
    return configs


# Stock HA panels that the GA tenant flow does not surface in the sidebar.
# Listed by panel name (= frontend route segment, i.e. what appears after
# the slash). If HA Core renames one of these in a future release, the
# matching call below silently no-ops (frontend.async_remove_panel doesn't
# raise on unknown names) — the others continue to work.
#
# The list is conservative — we keep:
#   - "lovelace"          (Übersicht — primary GA dashboard)
#   - "config"            (Einstellungen — admin-only, already gated)
#   - "developer-tools"   (Entwicklerwerkzeuge — admin-only, already gated)
#   - "profile"           (User profile)
#   - panel_custom        (anything our custom_components register)
#
# Drop:
#   - "energy"            (energy dashboard — GA has its own analytics)
#   - "logbook"           (Verlauf log)
#   - "history"           (Verlauf graphs)
#   - "media-browser"     (Medien)
#   - "todo"              (To-do-Listen)
#   - "map"               (Karte — not relevant for indoor iHost devices)
GA_HIDDEN_DEFAULT_PANELS: tuple[str, ...] = (
    "energy",
    "logbook",
    "history",
    "media-browser",
    "todo",
    "map",
)


def _hide_default_ha_panels(hass: HomeAssistant) -> None:
    """Remove HA's stock sidebar panels that don't fit the GA tenant flow.

    Idempotent: ``frontend.async_remove_panel`` is a no-op on a panel that
    has already been removed (or was never registered for this HA build).
    Catches per-panel exceptions so one missing entry can't block the others.

    Run twice:
      1. Immediately at setup — covers panels registered before us (most stock
         ones do).
      2. Again on ``EVENT_HOMEASSISTANT_STARTED`` — covers stock integrations
         that register their panel later in startup (e.g. ``todo`` lands
         alphabetically after ``greenautarky_onboarding`` and is registered
         in its own ``async_setup_entry``, so our early call would miss it).
    """
    def _remove_all() -> None:
        for panel in GA_HIDDEN_DEFAULT_PANELS:
            try:
                frontend.async_remove_panel(hass, panel)
                _LOGGER.debug("removed default HA panel: %s", panel)
            except Exception as e:
                # Don't let a single rename/refactor in HA Core take down setup.
                _LOGGER.warning("failed to remove panel %s: %s", panel, e)

    _remove_all()

    # `todo` (and any future late-registered stock panel) lands after our
    # async_setup completes. Listen once for HA-fully-started to sweep again.
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    async def _on_started(_event) -> None:
        _remove_all()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the panel so the wizard is accessible from the HA app."""
    panel_dir = Path(__file__).parent / "panel" / "dist"

    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(panel_dir), cache_headers=False)]
    )

    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="ga-onboarding-panel",
        sidebar_title="Einrichtung",
        sidebar_icon="mdi:rocket-launch",
        module_url=f"{URL_BASE}/entrypoint.js",
        embed_iframe=False,
        require_admin=False,
    )


def _patch_index_view_for_wizard_redirect(hass: HomeAssistant) -> None:
    """Monkey-patch HA's IndexView.get to redirect `/` → /greenautarky-setup.html
    while the GA wizard is incomplete.

    Why we have to monkey-patch instead of registering our own `/` view:
    `frontend` is a hard dependency of this integration, so by the time
    we call `_async_setup_common` the frontend integration has already
    registered IndexView at `/` and aiohttp resolves that route first.

    Why the existing `add_extra_js_url`-based redirect isn't enough:
    HA Core injects extra_module_url tags ONLY into the authenticated
    dashboard HTML (see homeassistant.components.frontend.IndexView.get).
    On a fresh device the first hit lands on `/onboarding.html` or
    `/auth/authorize` — neither carries our redirect script, so the
    customer never lands on the GA wizard automatically. Confirmed
    live on KIB-SON-31 bench on BOSv1.2.0 build #6.

    Behavior of the patch:
    - GA wizard incomplete → 302 to /greenautarky-setup.html
    - GA wizard complete → fall through to HA's original IndexView.get
      (which then handles HA-stock onboarding state + dashboard
       authentication as usual)

    Idempotent: marks `IndexView._ga_wizard_patched = True` on the
    class object so re-entry (e.g. an integration reload) doesn't
    re-wrap the already-wrapped method.

    Fleet safety:
    - Already-deployed devices that completed the wizard before this
      code shipped have `completed=True` in storage → patch is a no-op
      (falls through to original IndexView.get).
    - Old devices that never had a stored onboarding state have
      `completed=True` written by default in `_async_setup_common`
      (see the `state is None` branch) → also no-op.
    - The only behavioural change is on devices with
      `completed=False` in storage — which is precisely the "wizard
      pending" state where we WANT the redirect.
    """
    from functools import wraps

    from homeassistant.components.frontend import IndexView

    if getattr(IndexView, "_ga_wizard_patched", False):
        return

    original_get = IndexView.get

    @wraps(original_get)
    async def patched_get(self, request: web.Request) -> web.Response:
        state = _get_state(hass) or {}
        if not state.get("completed", False):
            return web.Response(
                status=302,
                headers={"location": "/greenautarky-setup.html"},
            )
        return await original_get(self, request)

    IndexView.get = patched_get  # type: ignore[method-assign]
    IndexView._ga_wizard_patched = True  # type: ignore[attr-defined]
    _LOGGER.info(
        "greenautarky_onboarding: patched IndexView.get to redirect `/` → "
        "/greenautarky-setup.html while wizard is incomplete (server-side, "
        "fires before any HA JS bundle loads)"
    )


def _register_redirect_js(hass: HomeAssistant) -> None:
    """Inject the `/` → wizard redirect as a client-side JS module.

    Replaces the IndexView patch from the GA HA Core fork. The aiohttp
    middleware approach (v0.1.x) does not work from a custom_component —
    the middleware list is frozen by the time we set up (Finding 20).

    `frontend.add_extra_js_url` is the blessed API for custom integrations
    to inject JS into the HA frontend. The module (`ga-onboarding-redirect.js`,
    served as a static path by `_async_register_frontend_bundle`) checks the
    onboarding status and redirects the browser to /greenautarky-setup.html
    while onboarding is incomplete.

    Trade-off vs. the server-side patch: the HA frontend bundle begins
    loading before the redirect fires, so there is a brief flash of the
    HA UI. Acceptable for a one-time onboarding flow.

    Defensive: `add_extra_js_url` indexes `hass.data["frontend_extra_module_url"]`,
    which only exists once the `frontend` component has set up. On a real
    device `frontend` is always present, but in minimal HA setups / unit
    tests it may not be — so a KeyError here is non-fatal: the wizard
    stays reachable directly at /greenautarky-setup.html.
    """
    try:
        add_extra_js_url(hass, REDIRECT_JS_URL)
    except KeyError:
        _LOGGER.warning(
            "frontend component not available — onboarding redirect JS not "
            "injected; wizard still reachable directly at /greenautarky-setup.html"
        )
        return
    _LOGGER.debug("Registered onboarding redirect JS module at %s", REDIRECT_JS_URL)
