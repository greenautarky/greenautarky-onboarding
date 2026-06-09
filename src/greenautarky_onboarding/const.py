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
