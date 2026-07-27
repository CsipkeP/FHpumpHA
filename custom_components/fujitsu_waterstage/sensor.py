"""Read-only sensors, one per register that is not a two-state indicator.

The mapping rules come from DESIGN.md section 9.1.  Nothing is guessed from a
register's name: the data type, the ``unit`` field and the ``options`` /
``options_ref`` fields of ``mbio_registers.json`` decide everything.

Status and error codes are resolved through the code tables of the register map
and published as text, with the raw controller code kept in the ``code``
attribute so automations and bug reports still have the number.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .codec import RegisterType
from .const import ATTR_CODE
from .coordinator import WaterstageRuntime
from .entity import WaterstageEntity
from .registers import Register

#: Seconds per hour -- runtime counters are published in hours (DESIGN.md 9.1).
_SECONDS_PER_HOUR = 3600

#: JSON ``unit`` -> (Home Assistant unit, device class, state class).
_UNITS: dict[str, tuple[str | None, SensorDeviceClass | None, SensorStateClass | None]] = {
    "%": (PERCENTAGE, None, SensorStateClass.MEASUREMENT),
    "min": (UnitOfTime.MINUTES, SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT),
    "h": (UnitOfTime.HOURS, SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT),
    "d": (UnitOfTime.DAYS, SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT),
    "months": ("months", None, SensorStateClass.MEASUREMENT),
    "baud": ("baud", None, None),
}


def is_two_state(register: Register) -> bool:
    """Whether this register belongs to the binary sensor platform.

    Exactly two options, one of which is 0.  The second condition matters: the
    RVS software version (440) and the cooling release (143) also have two
    options, but neither is an on/off pair.
    """
    return (
        register.type is RegisterType.UINT16
        and register.options is not None
        and len(register.options) == 2
        and 0 in register.options
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a sensor for every non-binary register being polled."""
    runtime: WaterstageRuntime = entry.runtime_data
    entities: list[WaterstageSensor] = []
    for register in runtime.registers:
        if is_two_state(register):
            continue
        coordinator = runtime.coordinator_for(register)
        if coordinator is None:  # pragma: no cover - every tier gets one
            continue
        entities.append(WaterstageSensor(runtime, entry, register, coordinator))
    async_add_entities(entities)


class WaterstageSensor(WaterstageEntity, SensorEntity):
    """A single decoded register value."""

    def __init__(self, runtime, entry, register, coordinator) -> None:  # noqa: ANN001
        super().__init__(runtime, entry, register, coordinator)
        self._coded = register.options is not None or register.options_ref is not None
        self._seconds_to_hours = False
        self._apply_measurement_traits()

    def _apply_measurement_traits(self) -> None:
        """Pick unit, device class and state class from the register map."""
        register = self.register

        if register.type is RegisterType.DTIME:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            return

        if self._coded:
            # A code resolved to text.  Only inline option lists are complete
            # enough for an enum device class; the shared status and error
            # tables have hundreds of entries and the board may report one that
            # is not in the manual, which would make the state invalid.
            if register.options is not None:
                self._attr_device_class = SensorDeviceClass.ENUM
                self._attr_options = list(dict.fromkeys(register.options.values()))
            return

        if register.type is RegisterType.TEMP:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            return

        if register.type is RegisterType.UINT32:
            # Runtimes come in seconds and are published in hours; the plain
            # counters have no unit at all.  Both only ever grow.
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            if register.unit == "s":
                self._seconds_to_hours = True
                self._attr_native_unit_of_measurement = UnitOfTime.HOURS
                self._attr_device_class = SensorDeviceClass.DURATION
                self._attr_suggested_display_precision = 1
            return

        unit, device_class, state_class = _UNITS.get(
            register.unit or "", (None, None, SensorStateClass.MEASUREMENT)
        )
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if isinstance(register.scale, float):
            # 0.01 for the heating curve slope, 0.1 for compressor starts.
            self._attr_suggested_display_precision = len(str(register.scale).split(".")[1])

    @property
    def native_value(self) -> str | int | float | datetime | None:
        """The decoded value, or the text behind a status/error code."""
        decoded = self.decoded
        if decoded is None or decoded.value is None:
            return None
        value = decoded.value

        if self.register.type is RegisterType.DTIME:
            assert isinstance(value, datetime)
            # The controller keeps local wall clock time and says nothing about
            # its zone, so Home Assistant's own zone is the best assumption.
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

        if self._coded:
            assert isinstance(value, int)
            # An unknown code is published as unknown rather than as a number:
            # with an enum device class a stray value would be an invalid state.
            return self.runtime.register_map.describe(self.register, value)

        if self._seconds_to_hours:
            assert isinstance(value, int)
            return value / _SECONDS_PER_HOUR

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Keep the raw code available even when it has no text."""
        if not self._coded:
            return None
        decoded = self.decoded
        if decoded is None or decoded.value is None:
            return None
        return {ATTR_CODE: decoded.value}
