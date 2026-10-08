"""A new resident starts with a 24-hour clock and day-month-year dates.

WHY
===
Home Assistant keeps a person's time and date format in their own frontend
user data, not in the site config: ``.storage/frontend.user_data_<user_id>``,
key ``"language"``, a ``FrontendLocaleData`` object (frontend
``src/data/translation.ts``)::

    {"language": "de", "number_format": "language",
     "time_format": "language" | "system" | "12" | "24",
     "date_format": "language" | "system" | "DMY" | "MDY" | "YMD", ...}

A user who never opened Profile has no such object, and the frontend falls back
to ``"language"`` — the format of the BROWSER's language. A resident whose
phone runs in English therefore sees 12-hour times on every chart
(``apexcharts-card`` reads ``hass.locale.time_format`` the same way). Reported
by Ahmad, 2026-10-08. There is no site-wide default for these two fields:
``frontend.system_data`` carries ``core``/``home``/``energy`` only.

WHAT
====
Every resident this component creates (the wizard's account step and a
household member joining by invite) gets ``time_format: "24"`` and
``date_format: "DMY"`` — through Core's own frontend user store, the object
the ``frontend/subscribe_user_data`` websocket reads, so an open browser sees
it at once.

A field is only written while it is UNSET or ``"language"`` (the frontend's
"auto"). ``"system"``, ``"12"``, ``"MDY"``, ``"YMD"`` — a choice somebody made —
is never touched. The same rule backfills existing users from ga_manager
(``resident_locale_defaults`` reconciler); keep the two in step.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

#: Frontend user-data key that holds ``FrontendLocaleData``.
LOCALE_USER_DATA_KEY = "language"

#: What a resident starts with.
RESIDENT_LOCALE_DEFAULTS: dict[str, str] = {
    "time_format": "24",
    "date_format": "DMY",
}

#: Values that mean "nobody chose": the field is missing, or the frontend's
#: "auto (use the language)" — which the frontend also stores on its own when
#: the user changes any OTHER locale field, so it is not a decision.
AUTO_VALUES: frozenset[str | None] = frozenset({None, "language"})


def locale_with_defaults(current: Any) -> dict[str, Any] | None:
    """The locale object with the resident defaults filled in, or None.

    None means: nothing to write (every field already carries a choice) or the
    stored value is not an object we understand — then it is left alone.
    """
    if current is None:
        current = {}
    if not isinstance(current, dict):
        return None
    updated = dict(current)
    for field, value in RESIDENT_LOCALE_DEFAULTS.items():
        if updated.get(field) in AUTO_VALUES:
            updated[field] = value
    return updated if updated != current else None


async def async_apply_resident_locale_defaults(
    hass: HomeAssistant, user_id: str
) -> dict[str, Any] | None:
    """Give ``user_id`` the resident time/date format where none is chosen.

    Returns the locale object written, or None if nothing was written. Never
    raises: a failed preference write must not cost a resident their new
    account — it is logged as an ERROR with the traceback instead, and the
    ga_manager reconciler picks the user up.
    """
    try:
        from homeassistant.components.frontend.storage import async_user_store

        store = await async_user_store(hass, user_id)
        current = store.data.get(LOCALE_USER_DATA_KEY)
        updated = locale_with_defaults(current)
        if updated is None:
            if current is not None and not isinstance(current, dict):
                _LOGGER.warning(
                    "resident locale: user %s has a %s under %r, not an object "
                    "— left as it is",
                    user_id, type(current).__name__, LOCALE_USER_DATA_KEY,
                )
            return None
        await store.async_set_item(LOCALE_USER_DATA_KEY, updated)
    except Exception:
        _LOGGER.exception(
            "resident locale: could not set the 24 h / DMY default for user %s "
            "— the resident keeps the browser-language format",
            user_id,
        )
        return None
    _LOGGER.info(
        "resident locale: user %s starts with time_format=%s date_format=%s",
        user_id, updated.get("time_format"), updated.get("date_format"),
    )
    return updated
