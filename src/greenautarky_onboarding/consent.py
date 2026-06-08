"""Consent management for greenautarky onboarding.

Tracks which policy versions users have accepted. When a policy version is
bumped in const.py, an HA repair issue is created so users are prompted to
re-confirm.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import CONSENT_TITLES, CONSENT_TYPES, DOMAIN

_LOGGER = logging.getLogger(__name__)


def get_outdated_consents(state: dict[str, Any]) -> dict[str, int]:
    """Return consent types where stored version < required version.

    Returns a dict of {consent_type: required_version} for outdated consents.
    """
    consents = state.get("consents", {})
    outdated: dict[str, int] = {}
    for consent_type, required_version in CONSENT_TYPES.items():
        stored = consents.get(consent_type, {})
        stored_version = stored.get("version", 0)
        if stored_version < required_version:
            outdated[consent_type] = required_version
    return outdated


def async_check_and_create_issues(hass: HomeAssistant, state: dict[str, Any]) -> None:
    """Check for outdated consents and create/remove repair issues."""
    outdated = get_outdated_consents(state)

    for consent_type in CONSENT_TYPES:
        issue_id = f"consent_outdated_{consent_type}"
        if consent_type in outdated:
            title = CONSENT_TITLES.get(consent_type, consent_type)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"consent_outdated_{consent_type}",
                data={"consent_type": consent_type},
            )
            _LOGGER.info("Consent outdated for %s — repair issue created", title)
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_record_consent(
    hass: HomeAssistant,
    store: Store[dict[str, Any]],
    state: dict[str, Any],
    consent_type: str,
) -> bool:
    """Record that a user has accepted the current version of a consent type.

    Returns True if the consent was recorded, False if the type is unknown.
    """
    if consent_type not in CONSENT_TYPES:
        return False

    consents = state.setdefault("consents", {})
    consents[consent_type] = {
        "version": CONSENT_TYPES[consent_type],
        "accepted_at": datetime.now(UTC).isoformat(),
    }
    await store.async_save(state)

    # Clear the repair issue
    ir.async_delete_issue(hass, DOMAIN, f"consent_outdated_{consent_type}")

    title = CONSENT_TITLES.get(consent_type, consent_type)
    _LOGGER.info("Consent accepted for %s (v%s)", title, CONSENT_TYPES[consent_type])
    return True
