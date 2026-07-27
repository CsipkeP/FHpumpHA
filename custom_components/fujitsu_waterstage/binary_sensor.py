"""Two-state registers.

Never assume the encoding.  Most on/off registers use ``0`` and **255**, the
room thermostats (128, 228) use ``0`` and ``1``, and the link status (0) is
``0`` for a BSB failure and ``1`` for OK.  The ``options`` field of
``mbio_registers.json`` is the only source of truth, so the "on" code is taken
from there per register (DESIGN.md section 9.1).
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_CODE, Control
from .coordinator import WaterstageRuntime
from .discovery import registers_for
from .entity import WaterstageEntity
from .registers import Register

_LOGGER = logging.getLogger(__name__)

#: Registers whose two states mean something more specific than on/off.
_DEVICE_CLASS_BY_NAME: dict[str, BinarySensorDeviceClass] = {
    "Off": BinarySensorDeviceClass.RUNNING,
    "No demand": BinarySensorDeviceClass.HEAT,
}


def on_code(register: Register) -> int:
    """The option code that means "on", straight from the register map."""
    return max(code for code in register.options if code != 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a binary sensor for every two-state register being polled."""
    runtime: WaterstageRuntime = entry.runtime_data
    async_add_entities(
        WaterstageBinarySensor(
            runtime, entry, register, runtime.coordinator_for(register)
        )
        for register in registers_for(
            runtime.registers, runtime.controls, Control.BINARY_SENSOR
        )
    )


class WaterstageBinarySensor(WaterstageEntity, BinarySensorEntity):
    """A register with exactly two documented states."""

    def __init__(self, runtime, entry, register, coordinator) -> None:  # noqa: ANN001
        super().__init__(runtime, entry, register, coordinator)
        self._on_code = on_code(register)
        if register.is_link_status:
            # 1 = the board can talk to the RVS21.  This is the switch the rest
            # of the integration hangs its availability on, so it gets the
            # connectivity class rather than a generic on/off.
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        else:
            self._attr_device_class = _DEVICE_CLASS_BY_NAME.get(register.options[0])

    @property
    def is_on(self) -> bool | None:
        """``True`` for the non-zero option code documented for this register."""
        decoded = self.decoded
        if decoded is None or decoded.value is None:
            return None
        if decoded.value not in self.register.options:
            _LOGGER.warning(
                "%s reported %s, which is not one of its documented states %s",
                self.register.key,
                decoded.value,
                sorted(self.register.options),
            )
            return None
        return decoded.value == self._on_code

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """The raw code, so an undocumented state is still visible."""
        decoded = self.decoded
        if decoded is None or decoded.value is None:
            return None
        return {ATTR_CODE: decoded.value}
