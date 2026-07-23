"""Constants for greenautarky onboarding."""

DOMAIN = "greenautarky_site"
STORAGE_KEY = "greenautarky_site"
STORAGE_VERSION = 2

# Steps in the onboarding wizard
STEP_PIN = "pin"
STEP_ACCOUNT = "account"
STEP_GDPR = "gdpr"
STEP_TELEMETRY = "telemetry"
STEP_ETHERNET = "ethernet"
STEP_INFO = "info"
STEP_COMPLETE = "complete"

# Physical access PIN. v1.0.3 moves the PIN file from `/config/ga-onboarding-pin`
# (= top-level, readable by any addon with `map: [config:rw]`) to
# `.storage/greenautarky_secrets/onboarding_pin`. Same filesystem but in
# HA Core's private dir by convention. Matches the v1.0.1 console-login
# secret move; threat-model is consistent across both files now.
# The legacy path is kept so the migration can find + delete it.
PIN_FILE = ".storage/greenautarky_secrets/onboarding_pin"  # relative to hass.config.path()
PIN_FILE_LEGACY = "ga-onboarding-pin"  # v1.0.0..1.0.2 path — migrate + delete
PIN_MAX_DELAY = 3600  # 1 hour cap for exponential backoff

# Consent types and their current required versions.
# Bump the version number to trigger re-consent for all users.
CONSENT_TYPES: dict[str, int] = {
    "gdpr": 1,
    "ethernet": 1,
}

# Human-readable titles for consent types (German)
CONSENT_TITLES: dict[str, str] = {
    "gdpr": "Datenschutzerklärung",
    "ethernet": "Ethernet-Verbindung",
}

# ─── Sub-user (household) management — ADR-0006 ────────────────────────────
# Master authorization flag. WRITTEN by ga_manager / ga-fleet-manager (they
# hold `config:rw`); this component only READS it. Lives in /config (durable,
# survives Core updates), NOT /share (writable by any addon). Plain JSON file:
#   {"masters": [{"ha_user_id": "<uuid>"}, ...]}
# Relative to hass.config.path() → /config/ga/ga-master-users.json
MASTER_USERS_FILE = "ga/ga-master-users.json"

# One-time, master-issued sub-user invite PINs. 6-digit numeric to reuse the
# onboarding wizard's PIN step (ga-setup-pin, 000-000 format) verbatim. Stored
# HASHED (sha256) in the onboarding Store with a TTL; consumed on first join.
# Brute force is infeasible: one-time use + short TTL + exponential backoff.
INVITE_PIN_ALPHABET = "0123456789"
INVITE_PIN_LENGTH = 6
SUB_USER_INVITE_DEFAULT_TTL_H = 24
SUB_USER_INVITE_MAX_TTL_H = 168  # 7 days
SUB_USER_JOIN_MAX_DELAY = 3600  # backoff cap (s) on bad join attempts
SUB_USER_MIN_PASSWORD_LEN = 8

# Sub-user data-protection consent at join (ADR-0006 open point → best-effort).
# A sub-user is a SEPARATE data subject from the device owner, so the join must
# capture its own Datenschutz consent (the device-level GDPR consent the owner
# gave does not cover them). BEST-EFFORT / privacy-review-gated: we require the
# consent at join and record who/when/what-version alongside the sub-user's
# parent bookkeeping in the onboarding Store. The review may relocate the record
# or bump the policy version (→ re-consent) without touching this flow.
#
# The consent version is standalone (not folded into CONSENT_TYPES, which tracks
# DEVICE-level consents keyed by type in `state["consents"]`); sub-user consent
# is PER data subject and lives under `state["sub_users"][<uid>]["consent"]`.
SUB_USER_CONSENT_VERSION = 1
# The privacy policy the sub-user consents to. Placeholder page stood up in
# parallel; kept in sync with the frontend link + the device GDPR step.
DATENSCHUTZ_URL = "https://greenautarky.com/datenschutz"
