"""Repairs integration for greenautarky onboarding.

Provides a fix flow for outdated consent issues. When the user clicks "Fix"
on a consent repair issue, they are redirected to the consent page.
"""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult


class ConsentRepairFlow(RepairsFlow):
    """Flow that redirects the user to the consent page."""

    async def async_step_init(self, user_input: None = None) -> FlowResult:
        """Redirect to the consent page as an external step."""
        return self.async_external_step(
            step_id="init",
            url="/greenautarky-consent",
        )

    async def async_step_complete(self, user_input: None = None) -> FlowResult:
        """Complete the repair flow after consent was accepted."""
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a fix flow for a consent issue."""
    return ConsentRepairFlow()
