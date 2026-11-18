"""The main-account password rule (Odoo #2052, EN 18031).

The sub-user join enforced a minimum length from day one; the primary account
step accepted any non-empty string, including a single space. Asserted at the
view, like the rest of the account-step suite.

  too short / whitespace-padded   refused with 400, NO user created
  exactly the minimum             accepted
  edge whitespace in a long one   accepted AND stored as typed, so the resident
                                  can log in with what they typed
"""

from __future__ import annotations

import pytest
from test_onboarding_account_step import (
    _count,
    _install_hass_auth_provider,
    _post,
    _seed,
)

from greenautarky_site.const import SUB_USER_MIN_PASSWORD_LEN

pytestmark = pytest.mark.usefixtures("pin_not_under_test")

ADDRESS = "resident@example.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    ["a", " ", "1234567", "   1234567   ", "abcdefg   "],
    ids=["one-char", "one-space", "seven-chars", "seven-padded-with-spaces", "seven-plus-trailing"],
)
async def test_a_short_or_padded_password_is_refused_and_creates_nothing(hass, secret) -> None:
    await _install_hass_auth_provider(hass)
    st = _seed(hass)
    resp = await _post(hass, ADDRESS, secret=secret)
    assert resp.status == 400
    assert _count(hass, "Resident") == 0, "a refused password still created a user"
    assert "account" not in st["steps_done"]


def test_the_minimum_is_eight() -> None:
    # Pinned constant: the rule is EN 18031 prep, not whatever const.py says today.
    assert SUB_USER_MIN_PASSWORD_LEN == 8


@pytest.mark.asyncio
async def test_exactly_eight_characters_is_accepted(hass) -> None:
    await _install_hass_auth_provider(hass)
    _seed(hass)
    resp = await _post(hass, ADDRESS, secret="12345678")
    assert resp.status == 200
    assert _count(hass, "Resident") == 1


@pytest.mark.asyncio
async def test_edge_whitespace_in_a_long_password_is_stored_as_typed(hass) -> None:
    await _install_hass_auth_provider(hass)
    _seed(hass)
    secret = "  long-enough-pw  "
    assert (await _post(hass, ADDRESS, secret=secret)).status == 200
    provider = next(p for p in hass.auth.auth_providers if p.type == "homeassistant")
    await provider.async_validate_login(ADDRESS, secret)  # raises InvalidAuth if trimmed on store
