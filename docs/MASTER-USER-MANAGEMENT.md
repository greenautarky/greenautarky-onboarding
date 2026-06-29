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

## Notes

- The master flag file `/config/ga/ga-master-users.json` is **written by us
  admins** via `ga_manager` / `ga-fleet-manager`; this component only **reads** it.
  It lives in `/config` (durable, survives Core updates), not `/share` (writable
  by any add-on → privilege escalation). See ADR-0006 for the full rationale.
