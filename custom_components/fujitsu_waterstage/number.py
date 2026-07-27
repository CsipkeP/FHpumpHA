"""Writable numeric registers.

Which registers get one is decided entirely by the write level, not by the
register map's ``R/W`` flag: at the default ``basic`` level that is three
numbers (reduced setpoint, heating curve displacement, summer/winter
changeover), because the other four of the seven writable data points are
covered by the climate and water heater entities (DESIGN.md 10.1).

The bounds and the step come from ``mbio_registers.json``, so the slider offers
exactly what the controller accepts.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .codec import RegisterType
from .const import Control
from .coordinator import MbioCoordinator, WaterstageRuntime
from .discovery import registers_for
from .entity import WaterstageWritableEntity
from .registers import Register
from .sensor import unit_traits

#: An unbounded register (the expert relay test) still needs a range; the raw
#: register width is the honest one.
_UINT16_MAX = 0xFFFF


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a number for every writable numeric register at this level."""
    runtime: WaterstageRuntime = entry.runtime_data
    async_add_entities(
        WaterstageNumber(runtime, entry, register, runtime.coordinator_for(register))
        for register in registers_for(runtime.registers, runtime.controls, Control.NUMBER)
    )


class WaterstageNumber(WaterstageWritableEntity, NumberEntity):
    """One writable numeric register."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
    ) -> None:
        super().__init__(runtime, entry, register, coordinator)

        scale = register.scale if isinstance(register.scale, float) else None
        self._attr_native_min_value = (
            register.minimum if register.minimum is not None else 0
        )
        self._attr_native_max_value = (
            register.maximum
            if register.maximum is not None
            else _UINT16_MAX * (scale or 1)
        )
        self._attr_native_step = register.step or scale or _default_step(register)

        if register.type is RegisterType.TEMP:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
        else:
            unit, device_class, _ = unit_traits(register)
            self._attr_native_unit_of_measurement = unit
            if device_class is not None:
                self._attr_device_class = NumberDeviceClass(device_class.value)

    @property
    def native_value(self) -> float | None:
        """The last read -- or just written -- value."""
        decoded = self.decoded
        if decoded is None or not isinstance(decoded.value, int | float):
            return None
        return decoded.value

    async def async_set_native_value(self, value: float) -> None:
        """Validate against the register map, then write one register."""
        await self.async_write(value)


def _default_step(register: Register) -> float:
    """Temperatures resolve to 0.1 °C, plain counts to 1."""
    return 0.1 if register.type is RegisterType.TEMP else 1
