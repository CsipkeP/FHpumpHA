"""Everything needed to debug an installation without touching it.

The point of this file is that a bug report should be enough (DESIGN.md section
12).  Two things in here do most of the work:

* the raw words of every read group, side by side with what they decoded to and
  whether the ``/O`` disable bit was set -- so a decoding bug is visible without
  guessing;
* the BSB error counters.  If those are climbing, the fault is in the BSB
  wiring between the interface board and the RVS21, not in Modbus and not in
  this integration.

The gateway address is redacted.  The board's serial number is not: it
identifies the hardware, not the household, and it is what a report needs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import (
    BOARD_IDENTIFICATION_ADDRESSES,
    BSB_ERROR_COUNTERS,
    BSB_UTILISATION_ADDRESSES,
    INTERFACE_ERROR_ADDRESS,
    LINK_STATUS_ADDRESS,
    MODBUS_ERROR_COUNTERS,
    POWER_ON_COUNTER_ADDRESS,
    UPTIME_ADDRESS,
)
from .coordinator import WaterstageRuntime
from .discovery import is_board_local, tier_for_register
from .entity import format_board_serial, format_rvs_version
from .registers import Register

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Any
) -> dict[str, Any]:
    """Dump the state of one interface board."""
    runtime: WaterstageRuntime | None = getattr(entry, "runtime_data", None)
    base: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "unique_id": _redact_unique_id(entry),
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        }
    }
    if runtime is None:
        base["error"] = "the config entry is not loaded"
        return base

    return {
        **base,
        "board": _board(runtime),
        "communication": _communication(runtime),
        "discovery": _discovery(runtime),
        "tiers": _tiers(runtime),
        "registers": _registers(runtime),
    }


def _redact_unique_id(entry: Any) -> str | None:
    """The unique id falls back to ``host:port:slave`` when there is no serial."""
    unique_id = entry.unique_id
    host = entry.data.get(CONF_HOST)
    if unique_id and host and host in unique_id:
        return unique_id.replace(host, REDACTED)
    return unique_id


def _value(runtime: WaterstageRuntime, address: int) -> Any:
    register = runtime.register_map.at(address)
    if register is None:
        return None
    decoded = runtime.decoded(register)
    return None if decoded is None else decoded.value


def _named(runtime: WaterstageRuntime, addresses: tuple[int, ...]) -> dict[str, Any]:
    """Values keyed by register name, so a report reads without the manual."""
    result: dict[str, Any] = {}
    for address in addresses:
        register = runtime.register_map.at(address)
        if register is None:  # pragma: no cover - the map is closed
            continue
        result[f"{address} {register.name}"] = _value(runtime, address)
    return result


def _board(runtime: WaterstageRuntime) -> dict[str, Any]:
    """Identification and uptime of the interface board itself."""
    return {
        "identification": _named(runtime, BOARD_IDENTIFICATION_ADDRESSES),
        "serial_number": format_board_serial(
            _value(runtime, 9902), _value(runtime, 9903)
        ),
        "rvs21_software_version": format_rvs_version(_value(runtime, 440)),
        "uptime_s": _value(runtime, UPTIME_ADDRESS),
        "power_on_count": _value(runtime, POWER_ON_COUNTER_ADDRESS),
        "read_function_code": hex(runtime.client.function_code),
        "slave_id": runtime.client.slave_id,
        "port": runtime.gateway.port,
        "connected": runtime.gateway.connected,
    }


def _communication(runtime: WaterstageRuntime) -> dict[str, Any]:
    """The link status, the board's error code, and every error counter."""
    link_register = runtime.register_map.at(LINK_STATUS_ADDRESS)
    link_value = _value(runtime, LINK_STATUS_ADDRESS)
    error_register = runtime.register_map.at(INTERFACE_ERROR_ADDRESS)
    error_value = _value(runtime, INTERFACE_ERROR_ADDRESS)

    return {
        "bsb_link": {
            "raw": link_value,
            "text": (
                runtime.register_map.describe(link_register, link_value)
                if link_register is not None and isinstance(link_value, int)
                else None
            ),
            "ok": runtime.link_ok,
        },
        "interface_error": {
            "raw": error_value,
            "text": (
                runtime.register_map.describe(error_register, error_value)
                if error_register is not None and isinstance(error_value, int)
                else None
            ),
        },
        # Modbus counters point at the RS-485 side, BSB counters at the cable to
        # the controller.  Which one grows says which half of the path is bad.
        "modbus_errors": _named(runtime, MODBUS_ERROR_COUNTERS),
        "bsb_errors": _named(runtime, BSB_ERROR_COUNTERS),
        "bsb_utilisation": _named(runtime, BSB_UTILISATION_ADDRESSES),
    }


def _discovery(runtime: WaterstageRuntime) -> dict[str, Any]:
    """Which blocks are on, which are not, and why."""
    result = runtime.discovery
    return {
        "write_level": runtime.write_level.value,
        "enabled_blocks": list(result.enabled),
        "excluded_blocks": {
            name: result.reasons.get(name, "not enabled in the options")
            for name in result.excluded
        },
        "room_sensors": list(result.room_sensors),
        "registers_polled": len(runtime.registers),
    }


def _tiers(runtime: WaterstageRuntime) -> dict[str, Any]:
    """Per tier: how it is doing, and the last raw answer of every group."""
    tiers: dict[str, Any] = {}
    for tier, coordinator in runtime.coordinators.items():
        tiers[tier.value] = {
            "interval_s": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "last_update_success": coordinator.last_update_success,
            "register_count": len(coordinator.registers),
            "failed_groups": dict(coordinator.group_errors),
            "groups": [
                {
                    "start": group.start,
                    "count": group.count,
                    "registers": len(group.registers),
                    # Hex, because that is how the manual writes them and how a
                    # disable bit is actually visible.
                    "raw": [
                        f"{word:04X}" for word in coordinator.raw.get(group.start, ())
                    ],
                }
                for group in coordinator.groups
            ],
        }
    return tiers


def _registers(runtime: WaterstageRuntime) -> dict[str, Any]:
    """Every polled register: raw words, decoded value and the disable flag."""
    registers: dict[str, Any] = {}
    for register in runtime.registers:
        coordinator = runtime.coordinator_for(register)
        decoded = runtime.decoded(register)
        registers[register.key] = {
            "address": register.address,
            "name": register.name,
            "type": str(register.type),
            "access": register.access,
            "tier": tier_for_register(register).value,
            "source": "board" if is_board_local(register) else "rvs21",
            "raw": _raw_words(coordinator, register),
            "value": None if decoded is None else decoded.value,
            "disabled": None if decoded is None else decoded.disabled,
            "text": _text(runtime, register, decoded),
            "controls": sorted(
                control.value for control in runtime.controls.get(register.key, ())
            ),
        }
    return registers


def _raw_words(coordinator: Any, register: Register) -> list[str] | None:
    """Slice this register's words out of its group's last raw response."""
    if coordinator is None:
        return None
    group = coordinator.group_for(register)
    if group is None:
        return None
    words = coordinator.raw.get(group.start)
    if words is None or len(words) != group.count:
        return None
    return [f"{word:04X}" for word in group.words_for(register, words)]


def _text(runtime: WaterstageRuntime, register: Register, decoded: Any) -> str | None:
    """The resolved status or error text, where the register has one."""
    if decoded is None or not isinstance(decoded.value, int):
        return None
    if register.options is None and register.options_ref is None:
        return None
    return runtime.register_map.describe(register, decoded.value)
