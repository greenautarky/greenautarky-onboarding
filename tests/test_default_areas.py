"""Tests for restoring the installation-default rooms (KB #169).

The hole these close: HA creates its three default areas only inside its own
onboarding step. The tenant wipe keeps `.storage/onboarding` marked done (so
the incoming tenant sees the GA wizard, not HA's) but wipes the area registry
(room names are tenant data). Nothing then recreates the rooms, and the
room-scoped dashboards have nothing to render.

The seeding has to be narrow in both directions — it must fire after a wipe,
and it must never resurrect rooms a living tenant deleted on purpose.
"""

from __future__ import annotations

import pytest
from homeassistant.helpers import area_registry as ar

from greenautarky_site.default_areas import async_seed_default_areas


@pytest.mark.asyncio
async def test_seeds_three_rooms_on_a_site_with_none(hass) -> None:
    created = await async_seed_default_areas(hass, {"completed": False})

    assert len(created) == 3
    names = {a.name for a in ar.async_get(hass).async_list_areas()}
    assert names == set(created)
    # Icons come along — a room with no icon is not "like at installation".
    assert all(a.icon for a in ar.async_get(hass).async_list_areas())


@pytest.mark.asyncio
async def test_is_a_noop_once_onboarding_is_complete(hass) -> None:
    """A tenant who deleted their own rooms must not find them back after a
    restart."""
    created = await async_seed_default_areas(hass, {"completed": True})

    assert created == []
    assert list(ar.async_get(hass).async_list_areas()) == []


@pytest.mark.asyncio
async def test_leaves_an_existing_site_alone(hass) -> None:
    registry = ar.async_get(hass)
    registry.async_create("Dachboden")

    created = await async_seed_default_areas(hass, {"completed": False})

    assert created == []
    assert {a.name for a in registry.async_list_areas()} == {"Dachboden"}


@pytest.mark.asyncio
async def test_is_idempotent(hass) -> None:
    first = await async_seed_default_areas(hass, {"completed": False})
    second = await async_seed_default_areas(hass, {"completed": False})

    assert len(first) == 3
    assert second == []
    assert len(list(ar.async_get(hass).async_list_areas())) == 3


@pytest.mark.asyncio
async def test_renamed_rooms_come_back_as_defaults_after_a_wipe(hass) -> None:
    """The end-to-end shape of what the operator asked for: a tenant renames a
    room, the wipe takes the registry with it, and the next start restores the
    default names — not the tenant's."""
    registry = ar.async_get(hass)
    await async_seed_default_areas(hass, {"completed": False})
    defaults = {a.name for a in registry.async_list_areas()}
    victim = next(iter(registry.async_list_areas()))
    registry.async_update(victim.id, name="Omas Zimmer")
    assert "Omas Zimmer" in {a.name for a in registry.async_list_areas()}

    # The wipe removes core.area_registry wholesale; mirror that here.
    for area in list(registry.async_list_areas()):
        registry.async_delete(area.id)
    # …and resets GA onboarding, which is what re-arms the seeding.
    await async_seed_default_areas(hass, {"completed": False})

    assert {a.name for a in registry.async_list_areas()} == defaults
    assert "Omas Zimmer" not in {a.name for a in registry.async_list_areas()}


@pytest.mark.asyncio
async def test_falls_back_when_ha_translations_are_unavailable(
    hass, monkeypatch
) -> None:
    """A missing translation catalogue must still produce rooms — a home with
    slightly off names beats a home with none."""
    import greenautarky_site.default_areas as mod

    async def _boom(*a, **kw):
        raise RuntimeError("no translations here")

    monkeypatch.setattr(
        "homeassistant.helpers.translation.async_get_translations", _boom
    )
    created = await mod.async_seed_default_areas(hass, {"completed": False})

    assert created == ["Wohnzimmer", "Küche", "Schlafzimmer"]
