/**
 * greenautarky onboarding — client-side redirect to the setup wizard.
 *
 * Injected into the Home Assistant frontend via
 * `frontend.add_extra_js_url()`. When GA onboarding is not yet completed,
 * it sends the browser to /greenautarky-setup.html.
 *
 * This replaces the server-side `/` redirect that used to live as an
 * IndexView patch in the greenautarky/ha-core fork. The middleware
 * approach (v0.1.x) does not work from a custom_component — aiohttp
 * freezes the middleware list before custom integrations set up
 * (see ga-ihost-docs/MIGRATION-CUSTOM-COMPONENT-FINDINGS.md, Finding 20).
 *
 * Trade-off vs. the server-side patch: the HA frontend bundle starts
 * loading before this redirect fires, so the user may see a brief flash
 * of the HA UI before landing on the wizard. Acceptable for onboarding.
 */
(async () => {
  "use strict";

  // The wizard is a standalone static page, not the HA SPA, so this
  // module is never loaded there — but guard defensively anyway.
  if (window.location.pathname.startsWith("/greenautarky-setup")) {
    return;
  }

  try {
    const resp = await fetch("/api/greenautarky_site/status", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    // Fail open: any non-OK response means we do NOT trap the user on a
    // page they can't leave. Better to show HA than to loop.
    if (!resp.ok) {
      return;
    }
    const state = await resp.json();
    if (state && state.completed === false) {
      window.location.replace("/greenautarky-setup.html");
    }
  } catch (err) {
    // Network error / JSON parse error → fail open, do nothing.
  }
})();
