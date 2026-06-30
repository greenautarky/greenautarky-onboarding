# Changelog

## Unreleased

### feat(sub-user): household sub-user join foundation (ADR-0006)

First slice of the Master-User Management Plane. A "Master-User" (a HA
Non-Admin flagged in `/config/ga/ga-master-users.json`, written by ga_manager
— read-only here, fail-closed) can mint **one-time, TTL-bounded invite PINs**.
Sub-users self-register via the **same link**, post-completion, through a new
**repeatable** route (not gated on `completed`, unlike the one-shot device
wizard), entering only **invite-PIN + password + display name**.

On redeem we mirror native HA onboarding: create a **Non-Admin** user
(`GROUP_ID_USER`) **and a linked Person** (empty — no `device_trackers`, so no
location; presence stays opt-in), auto-link the new user to the **issuing
master** (parent map in the onboarding Store), and consume the invite. Bad
invite attempts hit an exponential backoff; a revoked master invalidates
pending invites.

New endpoints: `POST /api/greenautarky_onboarding/sub_user/invite`
(master-only, authenticated), `GET /greenautarky-join` (page),
`POST /api/greenautarky_onboarding/sub_user/join` (invite-gated). Dashboard
assignment + the scoped management ops are a later increment (see ADR-0006).

Not deployed — design is privacy-review-gated before any device rollout.

## 1.0.4 — 2026-06-24

### feat(led): customer LED on/off endpoint (`GALedConfigView`)

New `GET`/`POST /api/greenautarky_onboarding/led` endpoint that reads and
persists a `led_disabled` preference into the onboarding HA Store
(`.storage/greenautarky_onboarding`). ga_manager's status-LED driver
(ga_manager 0.53.0) reads this flag and sets the iHost ring to `Off` when
the customer turns the status LED off; otherwise it drives the ring to
reflect device state (starting/connected/error). Settable any time
post-install (no onboarding-completion guard). The GACI app POSTs here
from its LED toggle.

## 1.0.3 — 2026-06-09

### Security — onboarding PIN moved to /config/.storage/

The physical-access PIN that gates wizard sign-up + password recovery
was stored at `/config/ga-onboarding-pin` in v1.0.0..1.0.2. That path
is readable by any Home Assistant addon that declares
`map: [config:rw]` in its `config.yaml` — a real exfil risk, and the
same threat-model issue that drove the v1.0.1 console-login secret move.

This release:

- Moves the PIN file from `/config/ga-onboarding-pin` to
  `/config/.storage/greenautarky_secrets/onboarding_pin` (= same
  directory used since v1.0.1 for the console-login HMAC secret).
- Adds an idempotent migration that runs once at integration setup
  (`_migrate_legacy_pin`), copies the legacy file over, removes it,
  and `chmod`s the new file 0600.
- If both files exist (= operator wrote the new one manually after a
  PIN rotation), the legacy file is removed and the new one preserved.
- Best-effort: a permission failure logs a warning, does NOT block
  integration setup. The `pin_required` view will reflect "no PIN" if
  the file ends up unreadable — operator can fix permissions.

The `.storage/` location is convention-private to HA Core. Addons that
respect HA's convention (= the overwhelming majority) won't see it
even with `[config:rw]`. Technically the only filesystem barrier
remains the same as before — but the convention drops the risk to
"deliberately misbehaving addon" instead of "any addon with config
access."

### Companion changes outside this repo
- `ha-operating-system` `tests/ga_tests/e2e_user_flows/test.sh` ships
  in the same Buildroot OS image that pins this release; its PIN_FILE
  path moved to the new location in the same commit.
- `ha-operating-system` `version.yaml` pins this release for the next
  OS build (BOSv1.2.8).

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
