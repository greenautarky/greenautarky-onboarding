"""Room-scoping plane: the room matrix + per-user strategy dashboard
(``rooms``), the Stage-A native entity boundary (``entity_scope``), and
the Stage-B read-path leak guard (``leak_guard``).

Moved out of the component root (#574 stage B) — re-exports only.
"""

from .rooms import (
    GAEntityScopingView,
    GAHomeModelView,
    GAMyRoomsView,
    GASubUserAssignRoomView,
    async_install_home_strategy,
    async_scope_for,
)

__all__ = [
    "GAEntityScopingView",
    "GAHomeModelView",
    "GAMyRoomsView",
    "GASubUserAssignRoomView",
    "async_install_home_strategy",
    "async_scope_for",
]
