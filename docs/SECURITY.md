# Security model — greenautarky-onboarding

## Secret storage

| Secret | Stored at | Notes |
|---|---|---|
| `console-login-secret` (HMAC key for operator auto-login) | `/config/.storage/greenautarky_secrets/console_login_secret` (0600, HA-Core-only) | Moved here in `1.0.1`. Old path: `/share/ga/console-login-secret` (= addon-readable). `_migrate_legacy_console_secret()` runs at integration setup and moves the file on first boot of `1.0.1+`. |
| `onboarding-pin` (printed device-label PIN, used for password recovery) | `hass.config.path(PIN_FILE)` → `/config/ga-onboarding-pin` (HA-Core-only) | Has always lived in `/config/` — `const.py:PIN_FILE = "ga-onboarding-pin"` is relative to `hass.config.path()`. |

`/share/` is mounted into **every** addon container by HA Supervisor.
A malicious or compromised customer-installed addon can read
everything in there. The `/config/` mount goes only into the HA Core
container — addons cannot reach it.

### Migration (= 1.0.1+ first-boot behaviour)

`_migrate_legacy_console_secret()` in `http.py` runs once at integration
setup:

1. If the new path exists → no-op (already migrated, or a fresh device
   was seeded directly into `/config/`). Stale `/share/` copy is still
   unlinked best-effort.
2. If only the legacy path exists → copy contents → chmod 0600 →
   unlink legacy file. Logged at INFO.
3. On `OSError` → log warning + continue. The auto-login view then
   returns 503 until an operator finishes the move.

Idempotent. Safe to run on every boot.

## Admin protection

`GAPasswordResetUsersView.post()` (in `http.py:864`) explicitly
filters out admin users:

```python
for user in await hass.auth.async_get_users():
    if user.system_generated:
        continue
    if user.is_admin:   # ← protected
        continue
    ...
```

This means the PIN-gated password reset can never set / reset the
admin (owner) password. Admin recovery is operator-only via
`GAConsoleLoginView` (signed-token auto-login) + manual change in
HA's Settings → People.

## Replay protection on signed-token flow

`GAConsoleLoginView` (`/api/ga_remote_login`) accepts tokens carrying a
random 8-byte nonce + 60-second `exp`. Inside the validity window the
view tracks seen nonces in an in-memory dict (`_SEEN_NONCES`,
pruned per request, ~5-min effective TTL). A URL re-replayed within
its `exp` returns HTTP 409.

## Rate limiting on PIN attempts

`GAPasswordResetUsersView` + `GAPasswordResetView` share an
exponential-backoff counter (`pw_reset_pin_attempts` + a
`pw_reset_pin_locked_until` timestamp in the integration's `Store`).
After 2 failed attempts the backoff starts at 5s and doubles, capped at
`PIN_MAX_DELAY` (defined in `const.py`).

## Threat model — what we DEFEND against

- Customer-installed addon trying to exfiltrate console-login secret
  (move to `/config/.storage/`)
- Customer-installed addon trying to brute-force the onboarding PIN
  (rate-limited + the PIN is read via a HA Core view — addon cannot
  call the view directly without auth)
- Operator URL leaked over Slack / email (60-second `exp` + single-use
  nonce burns the URL)
- Customer trying to elevate to admin via password reset (admin
  explicitly excluded from the resettable-users list)

## NOT defended against — out of scope

- Compromise of the fleet-manager (= signs the operator login token).
  If fleet-manager is owned, all device admin sessions are reachable.
- Physical possession of the device label (PIN is printed on it).
- Operators sharing the fleet-manager bearer token.
