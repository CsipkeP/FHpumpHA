"""Writable registers that hold one of a documented set of codes.

The option list comes from the ``options`` field of the register map, never
from a guess: the codes are not contiguous and not always zero-based -- the
cooling release (143) uses 1 and 2, the legionella weekday (46) uses 1 to 7.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_CODE, Control
from .coordinator import MbioCoordinator, WaterstageRuntime
from .discovery import registers_for
from .entity import WaterstageWritableEntity
from .registers import Register


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a select for every writable coded register at this level."""
    runtime: WaterstageRuntime = entry.runtime_data
    async_add_entities(
        WaterstageSelect(runtime, entry, register, runtime.coordinator_for(register))
        for register in registers_for(runtime.registers, runtime.controls, Control.SELECT)
    )


class WaterstageSelect(WaterstageWritableEntity, SelectEntity):
    """One writable register with a documented option list."""

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
    ) -> None:
        super().__init__(runtime, entry, register, coordinator)
        self._codes: dict[str, int] = {
            text: code for code, text in register.options.items()
        }
        self._attr_options = list(self._codes)

    @property
    def current_option(self) -> str | None:
        """The text for the current code, or nothing if it is undocumented."""
        decoded = self.decoded
        if decoded is None or not isinstance(decoded.value, int):
            return None
        return self.register.options.get(decoded.value)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """The raw code, so an undocumented state is still visible."""
        decoded = self.decoded
        if decoded is None or decoded.value is None:
            return None
        return {ATTR_CODE: decoded.value}

    async def async_select_option(self, option: str) -> None:
        """Write the code behind the chosen text."""
        code = self._codes.get(option)
        if code is None:
            raise ServiceValidationError(
                f"{option!r} is not one of {sorted(self._codes)}"
            )
        await self.async_write(code)
