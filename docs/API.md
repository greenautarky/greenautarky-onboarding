# HTTP API — `greenautarky_site`

*Generated from code truth 2026-07-23 (every `HomeAssistantView` in the
package; url/methods/auth extracted via AST — see the table's source modules).
Auth column: "token" = `requires_auth = True` (HA bearer token of the calling
user); "open" = `requires_auth = False` (endpoint does its own gating — PIN,
one-shot-onboarding, HMAC token — noted per row).*

Base prefix for JSON endpoints: **`/api/greenautarky_site`**. HTML pages are
top-level paths. `hass.callApi("get", "greenautarky_site/x")` from frontend JS.

## Onboarding wizard (`onboarding/wizard.py`, `pin.py`)

| Method+Path | Auth | What |
|---|---|---|
| GET `/greenautarky-setup` | open | The wizard HTML page (redirect target while onboarding incomplete). |
| GET `/admin` | open | Admin shortcut that bypasses the wizard redirect. |
| GET `/api/greenautarky_site/status` | open | Wizard progress: steps done, completed flag, PIN required? Polled by the wizard + e2e. |
| POST `/api/greenautarky_site/verify_pin` | open (PIN is the gate; exponential backoff) | Verify the 6-digit device-sticker PIN. |
| POST `/api/greenautarky_site/gdpr` | open (pre-completion only) | Record GDPR consent step. |
| POST `/api/greenautarky_site/telemetry` | open (pre-completion only) | Record telemetry consent choice. |
| POST `/api/greenautarky_site/ethernet` | open (pre-completion only) | Record Ethernet consent choice. |
| GET+POST `/api/greenautarky_site/led` | open | Read/set the iHost status-LED preference (ga_manager applies it). |
| POST `/api/greenautarky_site/create_user` | open (ONE-SHOT: 403 "already completed" after onboarding) | Create the initial tenant account. |
| POST `/api/greenautarky_site/complete` | open (pre-completion only) | Mark onboarding complete (registers panel teardown etc.). |
| POST `/api/greenautarky_site/reset` | **token** (admin) | Reset wizard state so onboarding re-runs. |

## Password reset (`onboarding/password_reset.py`)

| Method+Path | Auth | What |
|---|---|---|
| GET `/greenautarky-password-reset` | open | The reset HTML page. |
| POST `/api/greenautarky_site/password_reset/users` | open (PIN-gated, rate-limited) | List resettable (non-admin) users after PIN proof. |
| POST `/api/greenautarky_site/password_reset` | open (PIN-gated, rate-limited) | Reset a tenant user's password. Admin accounts are refused. |

## Consent re-confirmation (`consent_views.py`)

| Method+Path | Auth | What |
|---|---|---|
| GET `/greenautarky-consent` | open | Consent re-confirmation HTML page. |
| GET `/api/greenautarky_site/consent/status` | token | Which consent types are outdated for the caller. |
| POST `/api/greenautarky_site/consent/accept` | token | Accept a consent type at its current version. |

## Console login (`console_login.py`)

| Method+Path | Auth | What |
|---|---|---|
| GET `/api/ga_remote_login` | open (HMAC-signed single-use token, 5-min TTL, nonce replay guard) | Fleet-manager-minted operator auto-login. **Path deliberately NOT domain-prefixed** — unchanged by the #574 rename; fleet-manager mints these URLs. |

## Household — master + sub-users (`household/`)

All master-gated endpoints re-check the master allowlist server-side
(`_require_master`); the frontend's hiding of the Verwalten view is UI only.

| Method+Path | Auth | What |
|---|---|---|
| POST `/api/greenautarky_site/sub_user/invite` | token (master) | Mint a one-time invite PIN (TTL'd, hashed at rest). |
| GET `/greenautarky-join` | open | The join-mode wizard HTML page. |
| POST `/api/greenautarky_site/sub_user/join` | open (invite PIN is the gate) | Redeem invite → creates non-admin HA user + linked Person + personal dashboard. Body: `{invite_pin, name, password, datenschutz_consent: true}`. |
| GET `/api/greenautarky_site/sub_user/list` | token (master) | The master's sub-users + dashboards + areas (feeds ga-master-card). |
| POST `/api/greenautarky_site/sub_user/set_master` | token (admin) | Add/remove a user in the master allowlist. |
| POST `/api/greenautarky_site/sub_user/set_enabled` | token (master) | Enable/disable a sub-user's login. |
| POST `/api/greenautarky_site/sub_user/remove` | token (master) | Permanently remove one of the master's OWN sub-users (+ Person + dashboard). |
| POST `/api/greenautarky_site/sub_user/assign_dashboard` | token (master) | Assign/unassign a dashboard to a sub-user (visibility reconcile). |
| POST `/api/greenautarky_site/sub_user/rename_area` | token (master) | Rename a room via the area registry. |
| GET `/greenautarky-master` | open (page; data endpoints are master-gated) | Prototype master console HTML page. |

## Scoping (`scoping/rooms.py`)

| Method+Path | Auth | What |
|---|---|---|
| GET `/api/greenautarky_site/my_rooms` | token | `{scope, reason, areas, areas_exist, is_master}` for the CALLER. The scope decision, with its reason. |
| GET `/api/greenautarky_site/home_model` | token | The READY, scoped, states-validated dashboard model (#569): `{scope, reason, is_master, user_name, areas_exist, rooms[], roomless?}`. Per room: `{area_id, name, climate[], lights[], switches[], temps[], hums[], batts[]}`. **This shape is a pinned contract with ga-frontend-bundle** — change it only with the paired consumer tests. |
| POST `/api/greenautarky_site/sub_user/assign_room` | token (master) | Grant/revoke ONE room for ONE of the master's own sub-users: `{sub_user_id, area_id, assigned}`. Triggers Stage-A permission recompile. |
| GET+POST `/api/greenautarky_site/entity_scoping` | token (admin) | Read/toggle the entity-scoping flag (default ON since 1.8.0; only an UNSET flag defaults on). |

## Error conventions

- `403` — gate failed: onboarding already completed (`create_user`), PIN
  missing/wrong, caller is not master/admin.
- `400` — malformed/missing body fields (field names above are exact; the join
  endpoint's are `invite_pin` and `datenschutz_consent`).
- `429`-style backoff is expressed as 403 + retry-after payload on PIN routes.

## Renamed from `greenautarky_onboarding` (2.0.0, #574)

Clean break, no aliases: every `/api/greenautarky_onboarding/*` path above
became `/api/greenautarky_site/*` in 2.0.0. Callers (ga-frontend-bundle,
ga_manager, OS e2e suites) move in the same rollout wave. `/api/ga_remote_login`
and the HTML page paths (`/greenautarky-setup`, `/greenautarky-join`, …) are
unchanged.
