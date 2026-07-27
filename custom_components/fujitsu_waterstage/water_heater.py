"""Domestic hot water as one entity.

Operating mode (register 40), nominal setpoint (41) and the B3 tank sensor (63).
The controller's "eco" mode is exposed twice on purpose: as an operation mode
for people who think in the controller's terms, and as Home Assistant's away
mode, which is what an automation reaches for when the house is empty
(DESIGN.md 9.2).
"""

from __future__ import annotations

from homeassistant.components.water_heater import (
    STATE_ECO,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DHW_LABEL,
    DHW_MODE_ADDRESS,
    DHW_SETPOINT_ADDRESS,
    DHW_TEMPERATURE_ADDRESS,
    Control,
)
from .coordinator import MbioCoordinator, WaterstageRuntime
from .entity import WaterstageEntity
from .registers import Register

#: Operating mode codes of register 40, from the register map.
MODE_OFF = 0
MODE_ON = 1
MODE_ECO = 2

OPERATION_TO_MODE: dict[str, int] = {
    STATE_OFF: MODE_OFF,
    STATE_ON: MODE_ON,
    STATE_ECO: MODE_ECO,
}
MODE_TO_OPERATION = {code: state for state, code in OPERATION_TO_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the water heater if the DHW registers are writable at this level."""
    runtime: WaterstageRuntime = entry.runtime_data
    mode = runtime.register_map.at(DHW_MODE_ADDRESS)
    if mode is None or Control.WATER_HEATER not in runtime.controls.get(
        mode.key, frozenset()
    ):
        return
    async_add_entities(
        [WaterstageWaterHeater(runtime, entry, mode, runtime.coordinator_for(mode))]
    )


class WaterstageWaterHeater(WaterstageEntity, WaterHeaterEntity):
    """The hot water tank."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_operation_list = [STATE_OFF, STATE_ON, STATE_ECO]
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.AWAY_MODE
    )

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
    ) -> None:
        super().__init__(runtime, entry, register, coordinator)
        self._attr_name = DHW_LABEL
        self._setpoint = runtime.register_map.at(DHW_SETPOINT_ADDRESS)
        self._attr_min_temp = self._setpoint.minimum or 40.0
        self._attr_max_temp = self._setpoint.maximum or 65.0
        self._attr_target_temperature_step = self._setpoint.step or 1.0
        self._follow(DHW_SETPOINT_ADDRESS, DHW_TEMPERATURE_ADDRESS)

    def _value(self, address: int) -> float | int | None:
        register = self.runtime.register_map.at(address)
        if register is None:  # pragma: no cover - the map is closed
            return None
        decoded = self.runtime.decoded(register)
        if decoded is None or decoded.disabled or decoded.value is None:
            return None
        if isinstance(decoded.value, int | float):
            return decoded.value
        return None  # pragma: no cover - these registers are numeric

    @property
    def _mode(self) -> int | None:
        decoded = self.decoded
        if decoded is None or not isinstance(decoded.value, int):
            return None
        return decoded.value

    @property
    def current_temperature(self) -> float | None:
        """The B3 tank sensor."""
        return self._value(DHW_TEMPERATURE_ADDRESS)

    @property
    def target_temperature(self) -> float | None:
        return self._value(DHW_SETPOINT_ADDRESS)

    @property
    def current_operation(self) -> str | None:
        mode = self._mode
        return None if mode is None else MODE_TO_OPERATION.get(mode)

    @property
    def is_away_mode_on(self) -> bool | None:
        """Eco is the controller's away setting."""
        mode = self._mode
        return None if mode is None else mode == MODE_ECO

    async def async_set_temperature(self, **kwargs: float) -> None:
        """Write the nominal setpoint."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise ServiceValidationError("No temperature given")
        await self.runtime.async_write(self._setpoint, temperature)
        self.async_write_ha_state()

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        mode = OPERATION_TO_MODE.get(operation_mode)
        if mode is None:
            raise ServiceValidationError(
                f"{operation_mode!r} is not one of {self._attr_operation_list}"
            )
        await self.runtime.async_write(self.register, mode)
        self.async_write_ha_state()

    async def async_turn_away_mode_on(self) -> None:
        await self.async_set_operation_mode(STATE_ECO)

    async def async_turn_away_mode_off(self) -> None:
        await self.async_set_operation_mode(STATE_ON)
