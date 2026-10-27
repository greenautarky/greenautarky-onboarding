"""Which account the console login signs in as - and when it must refuse.

Like ``console_dest``, a module with NO imports, so the rules can be tested
without standing up Home Assistant.

The signed login token may carry ``as: "resident"``: the operator then sees the
device exactly as the resident does. The session is minted for the account the
GA onboarding recorded when it created it (``onboarding_account_user_ids`` in
the onboarding state), and for nothing else:

  * exactly one such account that still exists, is active and is not a system
    user -> that account
  * none, or more than one -> REFUSE with a message. Never a guess, and never
    a fall back to the service admin: a refusal is visible to the operator, a
    wrong account is not.

An unknown ``as`` value is refused too, so a newer signer cannot be silently
downgraded to a different session than the one it asked for.
"""

from __future__ import annotations

from typing import Any, Iterable

AS_RESIDENT = "resident"
ACCOUNT_MARKER_KEY = "onboarding_account_user_ids"


def parse_as(raw: object) -> str | None:
    """``None`` (absent) or ``"resident"``; anything else raises ValueError."""
    if raw is None:
        return None
    if raw == AS_RESIDENT and isinstance(raw, str):
        return AS_RESIDENT
    raise ValueError("unsupported 'as' value")


def record_account(state: dict[str, Any], user_id: str) -> None:
    """Remember that the GA onboarding created (or adopted) this account."""
    ids = state.get(ACCOUNT_MARKER_KEY)
    if not isinstance(ids, list):
        ids = []
        state[ACCOUNT_MARKER_KEY] = ids
    if user_id not in ids:
        ids.append(user_id)


def pick_resident(marked_ids: object, users: Iterable[Any]) -> tuple[Any | None, str]:
    """``(user, "")`` for exactly one eligible marked account, else ``(None, reason)``."""
    if not isinstance(marked_ids, list) or not marked_ids:
        return None, (
            "No onboarding-created account is recorded on this device "
            "(onboarded before the record existed, or not onboarded yet)."
        )
    wanted = {i for i in marked_ids if isinstance(i, str)}
    eligible = [
        u for u in users
        if getattr(u, "id", None) in wanted
        and getattr(u, "is_active", False)
        and not getattr(u, "system_generated", True)
    ]
    if not eligible:
        return None, "The account the onboarding created no longer exists or is not active."
    if len(eligible) > 1:
        return None, (
            f"{len(eligible)} onboarding-created accounts exist; refusing to guess "
            "which one is the resident."
        )
    return eligible[0], ""
