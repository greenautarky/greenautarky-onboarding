"""The defaults a fresh — or freshly wiped — site starts from (KB #169).

Two things a GA device must have on day one and after every reset: it speaks
German, and it has rooms. Neither survives a tenant wipe on its own.

**Language.** Nothing in the GA flow ever set ``hass.config.language``. The
wizard read a ``language`` field and dropped it on the floor, so every device
ran on Home Assistant's built-in ``"en"`` — invisible most of the time,
because the wizard, the strategy views and the master card are all hard-coded
German, but it decides every SERVER-side translation. Room names are one of
those, which is how it surfaced.

**Rooms.** Home Assistant creates its three default areas in exactly one
place: its own onboarding step (``components/onboarding/views.py``). The
tenant wipe deliberately keeps ``.storage/onboarding`` marked done, so the
incoming tenant sees only the GA wizard — while wiping
``core.area_registry``, because room names are tenant data (a tenant can
rename any room from the Verwalten tab, and "Omas Zimmer" says something
about the household). Nothing then recreated them: a reset device came back
with ZERO rooms and the room-scoped dashboards had nothing to render.

Both are applied only while GA onboarding is INCOMPLETE — that is what
"default" means. A device is in that state exactly twice: fresh from the
flasher, and just after a reset. An operator who later switches a device to
another language, or a tenant who deletes a room they do not want, is not
overruled on the next restart.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .const import SITE_DEFAULT_LANGUAGE

_LOGGER = logging.getLogger(__name__)

# Fallback if HA ever moves/renames its constant or ships no area translations
# for the site language. Keys mirror homeassistant.components.onboarding's
# DEFAULT_AREAS.
_FALLBACK_AREAS: tuple[tuple[str, str, str], ...] = (
    ("living_room", "Wohnzimmer", "mdi:sofa"),
    ("kitchen", "Küche", "mdi:stove"),
    ("bedroom", "Schlafzimmer", "mdi:bed"),
)


def _is_fresh_site(state: dict[str, Any] | None) -> bool:
    """True while GA onboarding has not been completed."""
    return not (state or {}).get("completed")


async def async_ensure_site_language(
    hass: HomeAssistant, state: dict[str, Any] | None
) -> str | None:
    """Set the site language to German on a fresh/reset device.

    Returns the language it applied, or None if it left things alone.

    Persisted through ``hass.config.async_update`` (= ``.storage/core.config``),
    so it survives restarts but NOT a wipe — which is correct: the wipe takes
    core.config with it, and the next boot lands here again.
    """
    if not _is_fresh_site(state):
        return None
    if hass.config.language == SITE_DEFAULT_LANGUAGE:
        return None
    try:
        await hass.config.async_update(language=SITE_DEFAULT_LANGUAGE)
    except Exception:
        _LOGGER.exception("site language: could not set %s", SITE_DEFAULT_LANGUAGE)
        return None
    _LOGGER.info("site language: set to %s (was unset/default)", SITE_DEFAULT_LANGUAGE)
    return SITE_DEFAULT_LANGUAGE


async def _async_default_area_specs(hass: HomeAssistant) -> list[tuple[str, str]]:
    """(name, icon) for HA's default areas, in the site language.

    Reads HA's own constant and translation catalogue so the result is what its
    onboarding would have produced for a German install. Falls back to the
    built-in list if either is unavailable — a room with a slightly off name
    beats a home with no rooms.
    """
    try:
        from homeassistant.components.onboarding.const import DEFAULT_AREAS
        from homeassistant.helpers.translation import async_get_translations

        # SITE_DEFAULT_LANGUAGE, not hass.config.language: the rooms are seeded
        # on the same boot the language is set, and a German product should not
        # depend on the ordering of those two to avoid shipping a "Living Room".
        translations = await async_get_translations(
            hass, SITE_DEFAULT_LANGUAGE, "area", {"onboarding"}
        )
        specs: list[tuple[str, str]] = []
        for area in DEFAULT_AREAS:
            name = translations.get(f"component.onboarding.area.{area.key}")
            if not name:
                # Missing catalogue entry → use our own name for that key
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

    No-op unless BOTH hold: GA onboarding is incomplete, and the area registry
    is empty. So a site that already has rooms — however it got them — is left
    exactly as it is.

    Idempotent, and safe to run before HA's own onboarding: that step guards on
    ``async_get_area_by_name``, so it skips the rooms we created rather than
    duplicating them.
    """
    if not _is_fresh_site(state):
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
        _LOGGER.info("default areas: site had none — seeded %s", ", ".join(created))
    return created


async def async_apply_site_defaults(
    hass: HomeAssistant, state: dict[str, Any] | None
) -> None:
    """Language first, then rooms — the room names are a translation lookup."""
    await async_ensure_site_language(hass, state)
    await async_seed_default_areas(hass, state)
