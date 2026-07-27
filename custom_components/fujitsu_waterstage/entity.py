"""Shared entity base and device registry wiring.

Two devices, linked with ``via_device`` so a diagnostic problem is visibly the
board's and not the heat pump's (DESIGN.md section 9.3):

* **Fujitsu Waterstage** -- everything the RVS21 controller produces.
* **Waterstage Modbus I/O Board** -- the interface registers (9900-9921) and
  the heat exchanger temperature (13), which the board measures itself.

The same split decides availability: a BSB failure between board and controller
invalidates the first device's entities and leaves the second device's alone.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .codec import DecodedValue
from .const import (
    DEFAULT_MODEL_HEAT_PUMP,
    DOMAIN,
    MANUFACTURER_BOARD,
    MANUFACTURER_HEAT_PUMP,
    MODEL_BOARD,
    RVS_VERSION_ADDRESS,
    SERIAL_HIGH_ADDRESS,
    SERIAL_LOW_ADDRESS,
    VERSION_ADDRESS,
)
from .coordinator import MbioCoordinator, WaterstageRuntime
from .discovery import is_board_local
from .registers import Register

#: Blocks whose entities are configuration or diagnostics, not measurements.
_DIAGNOSTIC_BLOCKS = frozenset({"interface", "faults", "relays"})


def board_device_id(entry: ConfigEntry) -> tuple[str, str]:
    """Device registry identifier of the interface board."""
    return (DOMAIN, f"{entry.entry_id}_board")


def heat_pump_device_id(entry: ConfigEntry) -> tuple[str, str]:
    """Device registry identifier of the heat pump."""
    return (DOMAIN, entry.entry_id)


def format_rvs_version(raw: int | None) -> str | None:
    """Register 440 reports 85 for V8.5."""
    if not raw:
        return None
    return f"V{raw // 10}.{raw % 10}"


def format_board_serial(high: int | None, low: int | None) -> str | None:
    """Serial number from registers 9902 and 9903.

    The manual documents 9902 as year/week and 9903 as a factory and sequence
    number but not how they are meant to be rendered, so both words are shown
    verbatim in hex rather than guessing at a format.
    """
    if not high and not low:
        return None
    return f"{high or 0:04X}{low or 0:04X}"


def heat_pump_device_info(
    entry: ConfigEntry, runtime: WaterstageRuntime | None = None
) -> DeviceInfo:
    """Device entry for the heat pump itself."""
    board_info = runtime.board_info if runtime else {}
    return DeviceInfo(
        identifiers={heat_pump_device_id(entry)},
        manufacturer=MANUFACTURER_HEAT_PUMP,
        model=DEFAULT_MODEL_HEAT_PUMP,
        name=entry.title,
        sw_version=format_rvs_version(board_info.get(RVS_VERSION_ADDRESS)),
    )


def board_device_info(
    entry: ConfigEntry, runtime: WaterstageRuntime | None = None
) -> DeviceInfo:
    """Device entry for the Modbus interface board."""
    board_info = runtime.board_info if runtime else {}
    return DeviceInfo(
        identifiers={board_device_id(entry)},
        manufacturer=MANUFACTURER_BOARD,
        model=MODEL_BOARD,
        name=f"{entry.title} Modbus I/O Board",
        sw_version=(
            f"{board_info[VERSION_ADDRESS]:#06x}"
            if board_info.get(VERSION_ADDRESS)
            else None
        ),
        serial_number=format_board_serial(
            board_info.get(SERIAL_HIGH_ADDRESS), board_info.get(SERIAL_LOW_ADDRESS)
        ),
        via_device=heat_pump_device_id(entry),
    )


def entity_category_for(register: Register) -> EntityCategory | None:
    """Keep settings and board diagnostics out of the main dashboard.

    Writable registers show up as read-only sensors until phase 4 gives them
    real controls; either way they are configuration, not measurements.
    """
    if register.block in _DIAGNOSTIC_BLOCKS:
        return EntityCategory.DIAGNOSTIC
    if register.writable or register.resettable:
        return EntityCategory.DIAGNOSTIC
    return None


class WaterstageEntity(CoordinatorEntity[MbioCoordinator]):
    """One register, one entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: WaterstageRuntime,
        entry: ConfigEntry,
        register: Register,
        coordinator: MbioCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self.runtime = runtime
        self.register = register
        # The register key never changes between releases, so neither does this.
        self._attr_unique_id = f"{entry.entry_id}_{register.key}"
        self._attr_name = register.name
        self._attr_entity_category = entity_category_for(register)
        self._attr_device_info = (
            board_device_info(entry, runtime)
            if is_board_local(register)
            else heat_pump_device_info(entry, runtime)
        )

    @property
    def decoded(self) -> DecodedValue | None:
        """The last decoded value, or ``None`` when the register was not read."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.register.key)

    @property
    def available(self) -> bool:
        """Unavailable on a dead bus, a dead BSB link or a disabled data point.

        A disabled ``/O`` parameter is not switched off in this installation, so
        its value is meaningless -- publishing the 0 behind the disable bit would
        be worse than publishing nothing (DESIGN.md section 3).
        """
        return self.runtime.register_is_available(self.register)
