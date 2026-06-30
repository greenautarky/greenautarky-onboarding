"""Constants for greenautarky onboarding."""

DOMAIN = "greenautarky_onboarding"
STORAGE_KEY = "greenautarky_onboarding"
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

# One-time, master-issued sub-user invite PINs. High-entropy, unambiguous
# alphabet (no 0/O/1/I). Stored HASHED (sha256) in the onboarding Store with a
# TTL; consumed on first successful join.
INVITE_PIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars
INVITE_PIN_LENGTH = 8  # 32^8 ≈ 1.1e12 combinations
SUB_USER_INVITE_DEFAULT_TTL_H = 24
SUB_USER_INVITE_MAX_TTL_H = 168  # 7 days
SUB_USER_JOIN_MAX_DELAY = 3600  # backoff cap (s) on bad join attempts
SUB_USER_MIN_PASSWORD_LEN = 8
