# greenautarky-site

GreenAutarky tenant onboarding wizard for Home Assistant — PIN-gated setup
flow, GDPR consent, password reset, signed-token operator auto-login,
default-panel hiding.

Ships as a Home Assistant `custom_components/` integration. Designed to be
installed via the OS image (`ha-operating-system` pulls a pinned version),
but is also installable on stock Home Assistant by copying
`src/greenautarky_site/` to `<config>/custom_components/`.

## What's in here

| Component | What |
|---|---|
| **Wizard** | 5-step onboarding (PIN → GDPR → Account → Telemetry → Ethernet → Complete). Customer-facing setup flow on a fresh GA device. |
| **Password reset** | PIN-gated reset for tenant users (admin accounts are explicitly protected). PIN is printed on the device label (static, immutable). |
| **Operator auto-login** | `/api/ga_remote_login` accepts an HMAC-signed token from the fleet-manager and lands the operator on the dashboard logged in as the HA owner — no password typing required. |
| **GDPR consent** | Tracked per consent-key + version. Repairs auto-issue when consents go stale. |
| **Sidebar cleanup** | Hides stock HA panels (energy / logbook / history / media-browser / todo / map) that the GA tenant flow doesn't surface. |
| **`/` redirect** | While the wizard is incomplete, all `/`-requests redirect to `/greenautarky-setup.html` (server-side IndexView patch). |

## Repository layout

```
src/greenautarky_site/
├── __init__.py              # setup wiring: store+migrations, view/panel registration
├── const.py                 # DOMAIN, STORAGE_KEY, wizard steps, consent versions
├── store.py                 # shared state accessors (_get_store/_get_state)
├── consent.py               # consent VERSIONING logic (pure, no HTTP)
├── consent_views.py         # consent re-confirmation HTTP surface
├── console_login.py         # HMAC signed-token operator auto-login
├── dashboards.py            # Lovelace dashboard helpers
├── onboarding/              # the device setup wizard (run once per site)
│   ├── wizard.py            #   step views (status/gdpr/led/telemetry/…)
│   ├── pin.py               #   device-sticker PIN verify + backoff
│   └── password_reset.py    #   PIN-gated tenant password reset
├── household/               # the people plane (master + sub-users)
│   ├── masters.py           #   master allowlist read/write/gate
│   ├── sub_users.py         #   invite/join/list/remove/set_enabled/set_master
│   └── dashboards_admin.py  #   dashboard assignment + visibility reconcile
├── scoping/                 # the visibility boundary
│   ├── rooms.py             #   room matrix, scope decision, home_model (#569)
│   ├── entity_scope.py      #   Stage A: native per-user entity permissions
│   └── leak_guard.py        #   Stage B: closes WS/REST side channels
├── config_flow.py           # HA config-flow shim
├── repairs.py               # repair-issue helpers (outdated consents etc.)
├── manifest.json            # HA integration manifest
├── *.html                   # standalone pages (consent / password-reset / …)
├── frontend_bundle/         # built wizard frontend (SHA256SUMS-verified)
└── BUILD.md                 # how the frontend bundle is (re)built

tests/                       # unit + integration (hass fixture) + e2e/ + device/
docs/
├── ARCHITECTURE.md          # the plane's map: packages, flows, seams  ← START HERE
├── API.md                   # every HTTP endpoint (method, auth, payload)
├── SECURITY.md              # secret-handling, /share-vs-/config notes
├── MASTER-USER-MANAGEMENT.md
└── STAGE-B-LEAK-WRAPPER.md
```

## Versioning

- `1.0.0` — initial extraction from `ha-operating-system/buildroot-external/rootfs-overlay/...`
  (2026-06-05). Includes `GAConsoleLoginView` (Phase 2 auto-login) +
  hide-default-panels.
- Future: `1.1.0` will add Option D (custom login page + recovery flow,
  fully replacing the dependency on a frontend fork for password recovery).

## Install

### Via `ha-operating-system` (production path)

The OS bakes a pinned version of this repo's content into the rootfs.
Bumping the OS version of the component is a one-line edit in
`ha-operating-system/version.yaml`:

```yaml
components:
  greenautarky-site: v1.0.0  # bump this
```

### Standalone on stock Home Assistant (dev / testing)

```bash
cd /config
git clone https://github.com/greenautarky/greenautarky-site.git
cp -r greenautarky-site/src/greenautarky_site custom_components/
# Add `greenautarky_site:` to configuration.yaml
# Restart HA
```

## Security notes

- **Secrets MUST NOT live in `/share/`** — that directory is mounted into
  every addon container. Use `/config/.storage/greenautarky_secrets/`
  (HA-Core-only readable) for: `console_login_secret`, `onboarding_pin`,
  any future per-device key.
- **PIN is printed on the physical device label** — static, immutable,
  customer-managed. Do NOT make it operator-rotatable.
- **Admin users cannot be reset via the customer-facing flow** —
  `GAPasswordResetUsersView` explicitly excludes `user.is_admin == True`
  from the resettable-users list. Admin recovery is operator-only via
  the signed-token auto-login flow + Settings → People → admin.

See [`docs/SECURITY.md`](docs/SECURITY.md) for the full threat model.

## Development

```bash
pip install -e .[dev]
pytest tests/ -v
ruff check src/ tests/
```

## License

Proprietary — © GreenAutarky GmbH. Not for redistribution.
