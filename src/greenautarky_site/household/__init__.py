"""Household plane — masters, sub-users, and dashboard administration
(ADR-0006).

Split out of the former ``http.py`` monolith (#574 stage B) — this file
only re-exports the public surface of the three modules.
"""

from .dashboards_admin import (
    GAMasterConsolePageView,
    GASubUserAssignDashboardView,
    GASubUserRenameAreaView,
    async_boot_register_personal_dashboards,
)
from .sub_users import (
    GASubUserInviteView,
    GASubUserJoinPageView,
    GASubUserJoinView,
    GASubUserManageView,
    GASubUserRemoveView,
    GASubUserSetEnabledView,
    GASubUserSetMasterView,
)

__all__ = [
    "GAMasterConsolePageView",
    "GASubUserAssignDashboardView",
    "GASubUserInviteView",
    "GASubUserJoinPageView",
    "GASubUserJoinView",
    "GASubUserManageView",
    "GASubUserRemoveView",
    "GASubUserRenameAreaView",
    "GASubUserSetEnabledView",
    "GASubUserSetMasterView",
    "async_boot_register_personal_dashboards",
]
