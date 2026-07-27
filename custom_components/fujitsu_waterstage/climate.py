"""One climate entity per heating circuit -- when there is a room sensor.

Without a room temperature the entity would show a target and no current
temperature, which reads as a broken thermostat rather than as a controller
running on a heating curve.  DESIGN.md 9.2 keeps the select-and-number pair for
those circuits instead, and :func:`~.discovery.assign_controls` makes that
decision so both platforms agree on who owns register 100.

The temperature range is not hard-coded: it is read from the circuit's own
frost protection and maximum comfort registers, so Home Assistant offers
exactly the span the controller will accept.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    HEATING_CIRCUITS,
    STATUS_CODES_HEATING,
    STATUS_CODES_IDLE,
    Control,
    HeatingCircuit,
)
from .coordinator import MbioCoordinator, WaterstageRuntime
from .entity import WaterstageEntity
from .registers import Register

#: Operating mode codes of register 100/200, from the register map.
MODE_PROTECTION = 0
MODE_AUTOMATIC = 1
MODE_REDUCED = 2
MODE_COMFORT = 3

#: Only three of the four modes map onto an HVAC mode.  "Reduced" is still
#: heating, just to the lower setpoint, so it reports HEAT and is reachable
#: through the preset instead.
HVAC_TO_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_PROTECTION,
    HVACMode.AUTO: MODE_AUTOMATIC,
    HVACMode.HEAT: MODE_COMFORT,
}
MODE_TO_HVAC: dict[int, HVACMode] = {
    MODE_PROTECTION: HVACMode.OFF,
    MODE_AUTOMATIC: HVACMode.AUTO,
    MODE_REDUCED: HVACMode.HEAT,
    MODE_COMFORT: HVACMode.HEAT,
}

PRESET_PROTECTION = "protection"
PRESET_AUTOMATIC = "automatic"
PRESET_REDUCED = "reduced"
PRESET_COMFORT = "comfort"

PRESET_TO_MODE: dict[str, int] = {
    PRESET_PROTECTION: MODE_PROTECTION,
    PRESET_AUTOMATIC: MODE_AUTOMATIC,
    PRESET_REDUCED: MODE_REDUCED,
    PRESET_COMFORT: MODE_COMFORT,
}
MODE_TO_PRESET = {code: preset for preset, code in PRESET_TO_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a climate entity for every circuit that claimed its registers."""
    runtime: WaterstageRuntime = entry.runtime_data
    entities: list[WaterstageClimate] = []
    for circuit in HEATING_CIRCUITS:
        mode = runtime.register_map.at(circuit.mode)
        if mode is None:
            continue
        if Control.CLIMATE not in runtime.controls.get(mode.key, frozenset()):
            continue
        entities.append(
            WaterstageClimate(
                runtime, entry, mode, runtime.coordinator_for(mode), circuit
            )
        )
    async_add_entities(entities)


class WaterstageClimate(WaterstageEntity, ClimateEntity):
    """A heating circuit: operating mode plus room comfort setpoint."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]
    _attr_preset_modes = [
        PRESET_PROTECTION,
        PRESET_AUTOMATIC,
        PRESET_REDUCED,
        PRESET_COMFORT,
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
        circuit: HeatingCircuit,
    ) -> None:
        super().__init__(runtime, entry, register, coordinator)
        self.circuit = circuit
        self._attr_name = circuit.label
        self._comfort = runtime.register_map.at(circuit.comfort)
        self._target_step = self._comfort.step or 0.5
        self._follow(
            circuit.comfort,
            circuit.frost_protection,
            circuit.max_comfort,
            circuit.status,
            circuit.room_temperature,
        )

    # -- helpers ----------------------------------------------------------

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

    # -- state ------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Room temperature.  Its presence is why this entity exists."""
        return self._value(self.circuit.room_temperature)

    @property
    def target_temperature(self) -> float | None:
        """The comfort setpoint.

        Deliberately always the comfort value, even while the circuit runs
        reduced: that is the number the user adjusts, and the reduced setpoint
        has its own entity.
        """
        return self._value(self.circuit.comfort)

    @property
    def target_temperature_step(self) -> float:
        return self._target_step

    @property
    def min_temp(self) -> float:
        """Frost protection setpoint, or the register map's lower bound."""
        value = self._value(self.circuit.frost_protection)
        return value if value is not None else (self._comfort.minimum or 4.0)

    @property
    def max_temp(self) -> float:
        """Maximum comfort setpoint, or the register map's upper bound."""
        value = self._value(self.circuit.max_comfort)
        return value if value is not None else (self._comfort.maximum or 35.0)

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._mode
        return None if mode is None else MODE_TO_HVAC.get(mode)

    @property
    def preset_mode(self) -> str | None:
        mode = self._mode
        return None if mode is None else MODE_TO_PRESET.get(mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Derived from the circuit's status code, not from the mode.

        The mode says what was asked for; the status says what the controller
        is doing about it.
        """
        status = self._value(self.circuit.status)
        if status is None:
            return None
        if status in STATUS_CODES_HEATING:
            return HVACAction.HEATING
        if status in STATUS_CODES_IDLE:
            return HVACAction.IDLE
        if self._mode == MODE_PROTECTION:
            return HVACAction.OFF
        return None

    # -- commands ---------------------------------------------------------

    async def async_set_temperature(self, **kwargs: float) -> None:
        """Write the comfort setpoint."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise ServiceValidationError("No temperature given")
        await self.runtime.async_write(self._comfort, temperature)
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Off means frost protection; heat means comfort."""
        mode = HVAC_TO_MODE.get(hvac_mode)
        if mode is None:
            raise ServiceValidationError(f"{hvac_mode} is not supported")
        await self.runtime.async_write(self.register, mode)
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """All four controller modes are reachable here."""
        mode = PRESET_TO_MODE.get(preset_mode)
        if mode is None:
            raise ServiceValidationError(f"{preset_mode!r} is not a known preset")
        await self.runtime.async_write(self.register, mode)
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
