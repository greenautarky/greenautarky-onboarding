"""Restore the installation-default rooms when a site has none (KB #169).

Home Assistant creates its three default areas — living room, kitchen,
bedroom — in exactly ONE place: its own onboarding ``users`` step
(``components/onboarding/views.py``). Nothing else ever recreates them.

That collides with the tenant wipe. The wipe deliberately keeps
``.storage/onboarding`` marked done, so the incoming tenant sees only the GA
wizard and not HA's welcome flow — but it wipes ``core.area_registry`` along
with the rest of the registries, because room NAMES are tenant data (a room
called "Omas Zimmer" says something about the household, and a tenant may
rename any room from the Verwalten tab).

The two together left a hole: after a wipe the site had zero rooms and HA
would never make new ones, so the next tenant landed in a home the room-scoped
dashboards could not render. This module closes it by seeding the same three
areas, from HA's own constant and translations, so "reset" really does mean
"like at installation" — default names, default icons, no leftovers of what
the previous tenant called them.

Deliberately narrow: it only ever acts when the site has NO areas at all AND
GA onboarding is incomplete. A tenant who deletes their own rooms mid-tenancy
does not get them resurrected on the next restart.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .const import SITE_DEFAULT_LANGUAGE

_LOGGER = logging.getLogger(__name__)

# Fallback if HA ever moves/renames the constant or ships no translation for
# the configured language. Keys mirror homeassistant.components.onboarding's
# DEFAULT_AREAS; the names are what a German-language install produces, which
# is what every GA device runs.
_FALLBACK_AREAS: tuple[tuple[str, str, str], ...] = (
    ("living_room", "Wohnzimmer", "mdi:sofa"),
    ("kitchen", "Küche", "mdi:stove"),
    ("bedroom", "Schlafzimmer", "mdi:bed"),
)


async def _async_default_area_specs(hass: HomeAssistant) -> list[tuple[str, str]]:
    """(name, icon) for HA's default areas, in the site's language.

    Reads HA's own constant + translation catalogue so the result is
    byte-identical to what its onboarding would have created. Falls back to
    the German defaults if either is unavailable — a seeded room with a
    slightly off name beats a home with no rooms.
    """
    try:
        from homeassistant.components.onboarding.const import DEFAULT_AREAS
        from homeassistant.helpers.translation import async_get_translations

        # NOT simply hass.config.language. The wipe takes `.storage/core.config`
        # with it, so after a reset that attribute is back to HA's built-in
        # "en" — which would hand a German household a "Living Room". Nothing
        # in the GA flow ever sets it either (the wizard reads a `language`
        # field and drops it). The product is German end to end, so that is the
        # default here; an explicitly configured non-English language still
        # wins, which is what a future localisation would set.
        language = hass.config.language
        if not language or language == "en":
            language = SITE_DEFAULT_LANGUAGE
        translations = await async_get_translations(
            hass, language, "area", {"onboarding"}
        )
        specs: list[tuple[str, str]] = []
        for area in DEFAULT_AREAS:
            name = translations.get(f"component.onboarding.area.{area.key}")
            if not name:
                # Unknown language → use the German fallback for this key
                # rather than dropping the room entirely.
                name = next(
                    (n for k, n, _ in _FALLBACK_AREAS if k == area.key), area.key
                )
            specs.append((name, area.icon))
        return specs
    except Exception as e:
        _LOGGER.warning(
            "default areas: HA constant/translations unavailable (%s) — "
            "using the built-in fallback",
            e,
        )
        return [(name, icon) for _, name, icon in _FALLBACK_AREAS]


async def async_seed_default_areas(
    hass: HomeAssistant, state: dict[str, Any] | None
) -> list[str]:
    """Create the default rooms if this site has none. Returns what it made.

    No-op unless BOTH hold:
      * GA onboarding is incomplete — i.e. a fresh device or one just wiped.
        During normal tenancy this module never touches the registry.
      * the area registry is empty — so a site that already has rooms, however
        it got them, is left exactly as it is.

    Idempotent, and safe to run before HA's own onboarding: that step guards
    on ``async_get_area_by_name``, so it will skip the rooms we created rather
    than duplicate them.
    """
    if (state or {}).get("completed"):
        return []

    registry = ar.async_get(hass)
    if list(registry.async_list_areas()):
        return []

    created: list[str] = []
    for name, icon in await _async_default_area_specs(hass):
        try:
            registry.async_create(name, icon=icon)
            created.append(name)
        except Exception as e:
            _LOGGER.warning("default areas: could not create %s: %s", name, e)

    if created:
        _LOGGER.info(
            "default areas: site had none — seeded %s", ", ".join(created)
        )
    return created
