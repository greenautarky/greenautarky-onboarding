# Stage B — the leak-wrapper (Odoo #516)

Stage A (`entity_scope.py`) installs a native per-user entity policy that Home
Assistant Core enforces on `get_states`, `subscribe_entities`, `call_service`
and the REST state API. Core does **not** check that policy on a handful of
other read paths, so a room-scoped sub-user can still learn about entities
outside their rooms through them. Stage B closes those.

## The leak surface (verified live on K49, 2026-07-20)

| WS command | leak | Stage-B action |
|---|---|---|
| `render_template` | a template can read any entity's state/attributes | **deny** for scoped users (a template cannot be safely output-filtered) |
| `history/history_during_period` | returns history for arbitrary `entity_ids` | **filter** the result to permitted entities |
| `logbook/get_events` | returns logbook rows for arbitrary entities | **filter** the result |
| `config/entity_registry/list` · `list_for_display` · `get_entries` | the full entity registry (names, areas, devices) | **filter** to permitted entities |
| `config/device_registry/list` | the full device registry | **filter** to devices that own a permitted entity |
| `config/area_registry/list` | every area name | **filter** to areas that contain a permitted entity |
| `history/stream` · `logbook/event_stream` | streaming variants | **prune the REQUEST** to permitted entities before delegation (deny when empty; a whole-home logbook stream gets the permitted list injected) |

`subscribe_entities` is already Stage-A-enforced (Core native) — not a Stage-B
concern. `get_states` / REST likewise.

## Mechanism

Home Assistant keeps registered WS commands in
`hass.data["websocket_api"]` as `{command: (handler, schema)}`, and
`async_register_command` **overwrites** an existing entry. So the guard, once
the target components have registered their commands, re-registers each one
with a wrapper that:

1. resolves `connection.user`;
2. **delegates untouched** when the user is an admin/owner or is *not*
   room-scoped (ground truth: membership in a `ga_scope_*` group — the exact
   thing Stage A adds and `async_clear` removes, so the guard tracks the
   applied policy, not just the config flag);
3. for a scoped user:
   - `render_template` → `send_error(unauthorized)`;
   - everything else → temporarily wrap `connection.send_result` so the
     handler's result is passed through a **per-command filter** keyed on
     `user.permissions.check_entity(entity_id, "read")` before it reaches the
     socket, then restore `send_result`.

The wrapper is marked (`_ga_leak_guarded = True`) so re-installing is
idempotent and never double-wraps.

## Filtering by the Stage-A policy, not a re-computation

The guard never recomputes "which rooms" — it asks the same authority Core
uses: `user.permissions.check_entity(entity_id, POLICY_READ)`. Registry rows
are filtered to permitted entities; device rows survive iff they own a
permitted entity; area rows survive iff they contain one. This guarantees the
guard and Core can never disagree.

## Timing & failure posture

Installed at HA start (after the leaky components have set up) and re-run by
the same boot reconcile that applies Stage A. Wrapping is wrapped in
try/except and logs — a guard failure must never take down the socket. When
scoping is disabled fleet-wide the guard is a no-op for every user (nobody is
in a `ga_scope_*` group), so it is safe to install unconditionally; it only
ever restricts a genuinely scoped sub-user.
