"""Shared accessors for the component's runtime state.

The onboarding Store + its in-memory state dict live in
``hass.data[DOMAIN]`` (wired by ``__init__._async_setup_common``);
every view module reads/writes through these helpers. The HA auth
provider lookup lives here too because both the onboarding and the
household plane create credentials through it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN


def _async_get_hass_provider(hass: HomeAssistant) -> HassAuthProvider:
    """Get the Home Assistant auth provider."""
    for prv in hass.auth.auth_providers:
        if prv.type == "homeassistant":
            return prv
    raise RuntimeError("Home Assistant auth provider not found")


def _get_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """Get the storage store."""
    return hass.data[DOMAIN]["store"]


def _get_state(hass: HomeAssistant) -> dict[str, Any]:
    """Get the current onboarding state."""
    return hass.data[DOMAIN]["state"]
