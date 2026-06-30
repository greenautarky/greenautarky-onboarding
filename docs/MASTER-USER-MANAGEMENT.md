# Master-User Management Plane — this component's role

> Design pointer. Authoritative design: **ADR-0006** in `ga-ihost-docs`
> (`adr/ADR-0006-master-user-management.md`). Odoo task
> [#427](https://greenautarky.odoo.com/odoo/project/17/tasks/427), KB
> [#96](https://greenautarky.odoo.com/odoo/knowledge/96). Status: **proposed**, not
> yet implemented.

## Why this repo

The Master-User is a Home Assistant **Non-Admin** and only has the HA frontend
(dashboards). HA's three roles (owner / admin / non-admin) offer no scoped
"household admin", and a Non-Admin cannot open Settings or add-on Ingress panels.
So the privileged management actions must run **inside Core**, not in the
`ga_manager` add-on.

**This component (`greenautarky_onboarding`) is the in-Core proxy.** It already
runs in-process in Core, exposes authenticated HTTP/WS endpoints
(see `http.py`, e.g. `create_user`) and persists state in `.storage` — the same
pattern the Master plane needs.

## Planned additions

- Scoped endpoints for the four Master ops, driven from a custom Lovelace card
  (shipped by `ga-frontend-bundle`):
  - create / remove sub-user → reuse `create_user` (`GROUP_ID_USER`), recorded
    under the master;
  - rename area → area_registry update;
  - rename entity (friendly name) → entity_registry update (name override);
  - assign dashboard → `.storage/lovelace_dashboards` visibility and/or per-view
    `visible`.
- **Authorization (the real security boundary):** on every call, verify the
  authenticated caller's UUID is in `/config/ga/ga-master-users.json` **and** that
  the target sub-user belongs to that master (parent relation). Never trust the
  UI. Masters may only touch their own sub-users; never Supervisor / OS / add-ons.
- The `[Sub-User × Dashboard]` matrix is held by this component (in `.storage`)
  and reconciled into HA-native dashboard visibility.

## Sub-user join sub-flow (decided 2026-06-30)

Sub-users self-register via the **same link** as device setup, but through a
**separate, repeatable** sub-flow that runs **after** `state["completed"] == true`
(the one-shot device wizard stays unchanged). The join page asks only **PIN +
password** (+ display name) and skips the device-level steps (GDPR / telemetry /
ethernet / info).

- **Gate = master-issued one-time invite PIN with TTL.** The master generates it
  from the Master card; store it like the existing onboarding PIN (under
  `.storage/greenautarky_secrets/`) with TTL + one-time-invalidate; reuse the
  `_check_pin_verified` backoff/lockout machinery.
- On valid PIN: create a **Non-Admin** user (`create_user`, `GROUP_ID_USER`) and
  **auto-link parent = the issuing master**, then invalidate the invite.
- **Mirror native onboarding: create a linked Person too.** After `create_user`,
  call `person.async_create_person(hass, name, user_id=user.id)` (exactly what
  `onboarding/views.py` does for the owner). 1:1 User + linked Person. The Person
  is created **empty (no `device_trackers`) → no location**; presence is opt-in.
  Permissions stay per-User; the Person is just the presence/automation layer.
- The new join route must **not** be gated on `completed == false` (today the
  redirect, sidebar panel, and IndexView fallthrough all key on `completed`).
- **Consent** is deferred to the privacy review — join is PIN+password only.

## Tests (required on implementation)

Security-sensitive — ship with tests: non-master rejected on every op;
parent-relation enforced (no cross-master access); flag missing/malformed fails
closed; invite-PIN one-time + TTL + backoff; join only post-completion + creates
Non-Admin + auto-parent; device wizard regression-safe; matrix → visibility.

Plus a **Playwright E2E** (after integration + build) mirroring the existing
onboarding E2E in `ha-operating-system/tests/e2e/tests/` (e.g.
`sub-user-join.spec.ts`): open the link, enter invite PIN + password, assert a
Non-Admin account is created, logs in, and sees only assigned dashboards.
See ADR-0006 §Testing requirements.

## Notes

- The master flag file `/config/ga/ga-master-users.json` is **written by us
  admins** via `ga_manager` / `ga-fleet-manager`; this component only **reads** it.
  It lives in `/config` (durable, survives Core updates), not `/share` (writable
  by any add-on → privilege escalation). See ADR-0006 for the full rationale.
