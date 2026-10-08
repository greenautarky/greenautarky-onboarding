"""The resident locale default fills only what nobody chose.

Must-flag: unset, ``"language"`` (the frontend's "auto") → 24 / DMY.
Must-pass: every explicit choice — ``"12"``, ``"system"``, ``"MDY"``,
``"YMD"`` — stays exactly as it is.
"""

from __future__ import annotations

import pytest

from greenautarky_site.locale_defaults import (
    async_apply_resident_locale_defaults,
    locale_with_defaults,
)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (None, {"time_format": "24", "date_format": "DMY"}),
        ({}, {"time_format": "24", "date_format": "DMY"}),
        (
            {"language": "en", "number_format": "language", "time_format": "language",
             "date_format": "language", "time_zone": "local", "first_weekday": "language"},
            {"language": "en", "number_format": "language", "time_format": "24",
             "date_format": "DMY", "time_zone": "local", "first_weekday": "language"},
        ),
        # one field chosen, the other auto: only the auto one moves
        ({"time_format": "12"}, {"time_format": "12", "date_format": "DMY"}),
        ({"date_format": "YMD", "time_format": "language"}, {"date_format": "YMD", "time_format": "24"}),
    ],
)
def test_auto_fields_get_the_resident_default(current, expected) -> None:
    assert locale_with_defaults(current) == expected


@pytest.mark.parametrize(
    "current",
    [
        {"time_format": "12", "date_format": "MDY"},
        {"time_format": "system", "date_format": "system"},
        {"time_format": "24", "date_format": "DMY"},
        "not-an-object",
        ["also", "not"],
    ],
)
def test_explicit_choices_are_never_overwritten(current) -> None:
    assert locale_with_defaults(current) is None


def test_the_input_is_not_mutated() -> None:
    current = {"time_format": "language"}
    locale_with_defaults(current)
    assert current == {"time_format": "language"}


async def test_apply_writes_through_cores_store_and_is_idempotent(hass, hass_storage) -> None:
    from homeassistant.components.frontend.storage import async_user_store

    user = await hass.auth.async_create_user("R")
    first = await async_apply_resident_locale_defaults(hass, user.id)
    assert first == {"time_format": "24", "date_format": "DMY"}
    assert hass_storage[f"frontend.user_data_{user.id}"]["data"]["language"] == first
    assert await async_apply_resident_locale_defaults(hass, user.id) is None
    assert (await async_user_store(hass, user.id)).data["language"] == first


async def test_apply_keeps_an_explicit_12h_choice(hass) -> None:
    from homeassistant.components.frontend.storage import async_user_store

    user = await hass.auth.async_create_user("R")
    store = await async_user_store(hass, user.id)
    await store.async_set_item("language", {"time_format": "12", "date_format": "MDY"})
    assert await async_apply_resident_locale_defaults(hass, user.id) is None
    assert store.data["language"] == {"time_format": "12", "date_format": "MDY"}
