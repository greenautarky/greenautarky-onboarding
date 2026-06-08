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

# Physical access PIN
PIN_FILE = "ga-onboarding-pin"  # relative to hass.config.path()
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
