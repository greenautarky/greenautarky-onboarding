# Changelog

## 1.0.2 — 2026-06-09

### Fixed — missing Store migration handler crashed setup
Setup of `greenautarky_onboarding` crashed with `NotImplementedError`
out of `homeassistant.helpers.storage._async_migrate_func` whenever a
v1 storage entry existed on disk. The integration shipped
`STORAGE_VERSION=2` and a `_migrate_v1_to_v2()` helper, but never wired
the helper into the `Store` class — HA's base implementation just raises
when it sees a stale version.

A new `_MigratableStore(Store)` subclass now overrides
`_async_migrate_func` to call `_migrate_v1_to_v2` for any `<2` major
version. `_async_setup_common` constructs that subclass instead of the
bare `Store`.

Caught by the new on-device E2E suite
`tests/ga_tests/e2e_user_flows/test.sh` on K31 BOSv1.2.6 (2026-06-09):
the suite wrote a v1 state file to simulate a fresh-provisioned device,
which exposed the missing migration handler.

## 1.0.1 — 2026-06-08

### Security
- **Move console-login HMAC secret to the HA-Core-only `/config/`** —
  previously stored at `/share/ga/console-login-secret`, which is
  mounted into every customer-installed addon (HACS or otherwise). Any
  addon could have read the secret and minted valid auto-login URLs
  for the device. The new path is
  `/config/.storage/greenautarky_secrets/console_login_secret` (0600,
  inside the HA Core container — not visible to addons).
- **Auto-migration on first boot of 1.0.1+** — `_migrate_legacy_console_secret`
  runs at integration setup, copies the legacy file into the new
  location, chmods it to 0600, and unlinks the legacy file so it can no
  longer be read from `/share/`. Idempotent (no-op if already migrated).
  Failure to migrate logs a warning, does NOT block setup — operators
  see a 503 from the auto-login view until they finish the move.
- Operator follow-up: re-issue the secret via fleet-manager → ga_manager
  converge if devices in the wild may have had it exfiltrated. The new
  location is also where fresh devices receive their seed.

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
