"""One-shot writes: counter resets and the three expert actions.

Two kinds of register end up here.

``R/Reset`` counters (runtimes, start counts, the outside temperature extremes)
and the interface's own error counters, which the register map calls ``R/W``
but which behave the same way -- any value clears them.  These are
irreversible but harmless, and they only appear from the ``advanced`` level.
The counter itself stays a sensor; the button is an extra, not a replacement.

The expert actions -- force a defrost, restart the heat pump, restart the
interface board -- write a specific documented value rather than zero, and only
exist when the expert write level is on.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EXPERT_ACTIONS, RESET_VALUE, Control
from .coordinator import MbioCoordinator, WaterstageRuntime
from .discovery import registers_for
from .entity import WaterstageEntity
from .registers import Register


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a button for every reset and expert action at this level."""
    runtime: WaterstageRuntime = entry.runtime_data
    async_add_entities(
        WaterstageButton(runtime, entry, register, runtime.coordinator_for(register))
        for register in registers_for(runtime.registers, runtime.controls, Control.BUTTON)
    )


class WaterstageButton(WaterstageEntity, ButtonEntity):
    """A single write with a fixed value."""

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
    ) -> None:
        super().__init__(runtime, entry, register, coordinator)
        action = EXPERT_ACTIONS.get(register.address)
        if action is not None:
            self._value = action[0]
            self._attr_entity_category = None  # a deliberate action, not config
        else:
            self._value = RESET_VALUE
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Write the action value."""
        await self.runtime.async_press(self.register, self._value)
