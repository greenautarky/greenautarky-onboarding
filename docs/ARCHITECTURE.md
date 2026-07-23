# Architecture — `greenautarky_site`

*The deployment-site management plane. Renamed from `greenautarky_onboarding`
2026-07-23 (Odoo #574) — the component long outgrew "onboarding": it manages
everything about ONE deployed GA device ("site" — a home, an office, any
installation) and the people who use it.*

## The one-paragraph mental model

Every GA device is **one site**. A site has an **operator-facing setup story**
(the onboarding wizard, run once), a **people story** (one *master* user who
runs the site, plus *sub-users* they invite), and a **visibility story** (which
rooms/entities each sub-user may see and control — enforced server-side). This
component owns all three. The frontend (ga-frontend-bundle: `ga-home` strategy,
`ga-master-card`) is deliberately dumb — every decision about *who may see
what* is made HERE, on the server, and handed to the frontend as data.

## Package map

```
src/greenautarky_site/
├── __init__.py          Setup wiring ONLY: store load + migrations, view
│                        registration, panel/static/redirect registration,
│                        boot-deferred reconciles. No business logic.
├── const.py             DOMAIN, STORAGE_KEY, wizard steps, consent versions.
├── store.py             The shared state accessors (_get_store/_get_state) +
│                        auth-provider lookup. Every module reads state
│                        through these — the one seam everyone shares.
├── consent.py           Consent VERSIONING logic (which consents exist, which
│                        are outdated). Pure logic, no HTTP.
├── consent_views.py     The consent re-confirmation HTTP surface.
├── console_login.py     Signed-token operator auto-login (fleet-manager mints
│                        an HMAC console-login URL; this view redeems it).
├── dashboards.py        Lovelace dashboard create/delete/registration helpers.
├── onboarding/          THE DEVICE SETUP WIZARD (run once per site)
│   ├── wizard.py        Step views: status/gdpr/led/telemetry/ethernet/
│   │                    complete/create_user/reset + page + admin bypass.
│   ├── pin.py           Device-sticker PIN: file location, migration,
│   │                    verification + backoff.
│   └── password_reset.py PIN-gated password reset for tenant users.
├── household/           THE PEOPLE PLANE (master + sub-users)
│   ├── masters.py       The master allowlist (/config/ga/ga-master-users.json):
│   │                    read/write/check + the _require_master gate.
│   ├── sub_users.py     Sub-user lifecycle: invite (PIN+TTL) → join (creates
│   │                    non-admin HA user + linked Person) → set_master /
│   │                    list / remove / set_enabled. Orphan handling.
│   └── dashboards_admin.py Per-sub-user dashboard assignment + visibility
│                        reconcile + area rename + master console page.
└── scoping/             THE VISIBILITY BOUNDARY (what a sub-user can see)
    ├── rooms.py         Room matrix ({user: [area]}), the scope decision
    │                    (async_scope_for), the ga-home strategy install, and
    │                    the server-side home_model endpoint (#569).
    ├── entity_scope.py  Stage A: compiles room assignments into NATIVE HA
    │                    per-user entity permissions (policy groups).
    └── leak_guard.py    Stage B: closes the side channels Stage A misses
                         (render_template, history/logbook streams, registry
                         lists, REST history) for scoped users.
```

## The load-bearing rule

**Only a real sub-user is ever restricted.** The scope decision
(`scoping/rooms.py: async_scope_for`) returns `all` for: an unmanaged device
(no master, no sub-users — most of the fleet), a master, an admin/owner, and a
legacy tenant. It returns `rooms` only for a user in the `sub_users` map. A
device we never configured can therefore never be made poorer by this
component. Every view and every reconcile preserves this.

## Request → scope → model (the resident dashboard flow, #569)

```
browser loads dashboard (strategy: {type: "custom:ga-home"})
  → ga-home strategy (ga-frontend-bundle) fetches
      GET /api/greenautarky_site/home_model            (auth: the user's token)
  → GAHomeModelView re-runs async_scope_for(user)      (never trusts the client)
  → _build_home_model walks the entity registry with the SERVER's full hass,
      keeps an entity iff: not hidden/disabled
                        AND it is a LIVE state
                        AND (scoped user → user.permissions.check_entity(read))
  → response: { scope, reason, is_master, user_name,
                rooms: [{area_id, name, climate[], lights[], switches[],
                         temps[], hums[], batts[]}], roomless? }
  → the strategy ONLY renders. It never touches the registries.
```

Why server-side: a scoped sub-user's (leak-guard-filtered) registry can list
entities absent from their scoped `hass.states` → a client-side derivation
rendered null-state cards and crashed the board (K0, 2026-07-22). The server
model makes that class of bug structurally impossible. The response shape is a
**pinned contract**: producer tests here (`tests/test_rooms.py:
test_home_model_*`) + consumer tests in ga-frontend-bundle
(`test_seam_*_read_verbatim`) fail on any field rename.

## The two scoping stages (both ON by default since 1.8.0)

- **Stage A (`entity_scope.py`)** — the real boundary: per-user HA policy
  groups computed from the room matrix. HA itself then refuses state reads and
  service calls outside the granted set.
- **Stage B (`leak_guard.py`)** — closes what policy groups don't cover:
  `render_template`, history/logbook websocket streams, registry list
  responses, REST `/api/history/period`.

Do not sell room scoping as tenant isolation unless BOTH stages are on
(see `docs/STAGE-B-LEAK-WRAPPER.md`).

## State & persistence

| What | Where | Owner |
|---|---|---|
| Site state (completed, consents, sub_users, sub_user_areas, invites, dashboards) | `.storage/greenautarky_site` (Store v2, `_MigratableStore`) | this component — a live `.storage` edit is overwritten on next save; edit only with Core stopped |
| Master allowlist | `/config/ga/ga-master-users.json` | this component (written via `household/masters.py`); ALSO written by ga_manager's set_master worker |
| Device PIN | `.storage/greenautarky_secrets/onboarding_pin` | provisioning (read-only here) |
| Console-login HMAC secret | `.storage/greenautarky_secrets/` | fleet-manager seeds it (console-secret-write job) |

**Storage migrations** (all in `__init__.py`): v1→v2 adds the consents dict;
the **rename migration** moves `.storage/greenautarky_onboarding` →
`.storage/greenautarky_site` once (MOVE, not copy — exactly one source of
truth; a stale copy would hold personal data the tenant-wipe could miss).

## Seams to other systems (keep these in lockstep)

| Counterpart | The seam | Breaks if |
|---|---|---|
| ga-frontend-bundle | `/api/greenautarky_site/home_model` + `sub_user/*` (strategy + master-card) | API path or home_model field renamed |
| ga_manager (addon) | copies the component dir, injects `greenautarky_site:` YAML key, reads/wipes `.storage/greenautarky_site`, LED prefs, wizard trigger | dir name / storage key / DOMAIN changed |
| ga-fleet-manager | `GA_COMPONENTS` OCI list (`greenautarky-site`), console-login secret seeding, `/api/ga_remote_login` | OCI artifact name changed |
| ha-operating-system | `version.yaml components.greenautarky-site` pin → `oras pull` at build | version key / artifact name changed |
| frontend fork (wizard JS) | built bundle hardcodes `/api/greenautarky_site/*` + `/greenautarky_site_static/*` | API/static prefix changed without a bundle rebuild |

Any rename in this table is a **coordinated, same-wave change** across repos —
see the #574 rollout notes in CHANGELOG 2.0.0.

## Import discipline

`store.py` and `const.py` are imported by everyone and import nothing internal.
`scoping/` imports from `store` + `household` at module level; `household` and
`onboarding` reach *into* `scoping` only lazily (inside handlers) — so the old
`http.py ↔ rooms.py` cycle is structurally gone. If you add a module-level
import that closes a cycle, Python will tell you at boot; keep the lazy edges
documented in-line.
