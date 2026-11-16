"""Import-only config flow: the seed creates its own entry on first start."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from . import DOMAIN


class SeedFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="GA e2e seed", data={})
