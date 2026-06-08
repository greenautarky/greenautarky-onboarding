# Changelog

## 1.0.0 — 2026-06-05 (planned)

Initial extraction of `greenautarky_onboarding` from
`ha-operating-system/buildroot-external/rootfs-overlay/...` into its own
repo, paired with the move to a Tier-2 component pattern (see
[ha-operating-system docs / decoupling proposal]).

### Carried over from the rootfs-overlay version
- Wizard flow (PIN → GDPR → Account → Telemetry → Ethernet → Complete).
- `GAPasswordReset*` views — PIN-gated tenant-user password reset
  (admin accounts explicitly protected).
- `GAOnboardingPageView`, `GAAdminBypassView`, status/GDPR/Telemetry/
  Ethernet/Complete/CreateUser/Reset/PinVerify views.
- `GAConsoleLoginView` — signed-token operator auto-login (shipped
  2026-06-05 from `feat/console-login-view`).
- `_hide_default_ha_panels` — drops `energy`/`logbook`/`history`/
  `media-browser`/`todo`/`map` from the sidebar.
- `_patch_index_view_for_wizard_redirect` — server-side `/` →
  `/greenautarky-setup.html` while wizard is incomplete.
- GDPR consent tracking + repair-issue helpers.

### Not yet
- Option D (custom login page + recovery flow) — target `1.1.0`.
- Secret refactor `/share/` → `/config/.storage/` — target `1.0.1`.

### Architectural notes
- Stops shipping the integration through two paths (the legacy
  `greenautarky/ha-core` fork copy is now deprecated; the rootfs-overlay
  copy will be removed once `ha-operating-system` consumes the OCI
  artifact published by this repo's release CI).
- Tests no longer require building the full OS image — `pytest tests/`
  runs in seconds against `pytest-homeassistant-custom-component`
  fixtures.
