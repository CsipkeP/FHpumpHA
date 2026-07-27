"""Fujitsu Waterstage heat pump over an FWS-MBIO-002 Modbus interface board."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_BLOCKS,
    CONF_DISCOVERY_REASONS,
    CONF_FRAMING,
    CONF_FUNCTION_CODE,
    CONF_INTER_REQUEST_DELAY_MS,
    CONF_MAX_REGISTERS,
    CONF_RETRIES,
    CONF_ROOM_SENSORS,
    CONF_SCAN_INTERVAL_BY_TIER,
    CONF_SLAVE_ID,
    CONF_TIMEOUT,
    CONF_WRITE_LEVEL,
    DEFAULT_INTER_REQUEST_DELAY_MS,
    DEFAULT_MAX_REGISTERS,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WRITE_LEVEL,
    PRODUCT_CODE_ADDRESS,
    RVS_VERSION_ADDRESS,
    SERIAL_HIGH_ADDRESS,
    SERIAL_LOW_ADDRESS,
    SETUP_SECOND_READ_DELAY,
    TIER_REFRESH_LIMIT,
    VERSION_ADDRESS,
    Tier,
    WriteLevel,
)
from .coordinator import MbioCoordinator, WaterstageRuntime, async_link_watcher
from .discovery import (
    DiscoveryResult,
    assign_controls,
    find_room_sensors,
    select_registers,
    tier_registers,
)
from .entity import board_device_info, heat_pump_device_info
from .hub import (
    DEFAULT_FRAMING,
    FUNCTION_READ_HOLDING,
    MbioClient,
    async_get_gateway,
    async_release_gateway,
)
from .registers import load_register_map

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.WATER_HEATER,
]

#: Registers copied into the device registry entries.
_BOARD_INFO_ADDRESSES = (
    PRODUCT_CODE_ADDRESS,
    VERSION_ADDRESS,
    SERIAL_HIGH_ADDRESS,
    SERIAL_LOW_ADDRESS,
    RVS_VERSION_ADDRESS,
)

type WaterstageConfigEntry = ConfigEntry[WaterstageRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: WaterstageConfigEntry) -> bool:
    """Connect to the board and start the four polling tiers."""
    register_map = await hass.async_add_executor_job(load_register_map)
    options = entry.options

    gateway = await async_get_gateway(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        framing=entry.data.get(CONF_FRAMING, DEFAULT_FRAMING),
        timeout=float(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        retries=int(options.get(CONF_RETRIES, DEFAULT_RETRIES)),
        inter_request_delay=int(
            options.get(CONF_INTER_REQUEST_DELAY_MS, DEFAULT_INTER_REQUEST_DELAY_MS)
        )
        / 1000,
    )
    # A gateway has one framing; an entry added before it was probed falls back
    # to the common case rather than guessing per request.
    await gateway.async_set_framing(entry.data.get(CONF_FRAMING, DEFAULT_FRAMING))
    client = MbioClient(
        gateway,
        entry.data[CONF_SLAVE_ID],
        function_code=entry.data.get(CONF_FUNCTION_CODE, FUNCTION_READ_HOLDING),
    )

    write_level = WriteLevel(options.get(CONF_WRITE_LEVEL, DEFAULT_WRITE_LEVEL))
    blocks = options.get(CONF_BLOCKS) or list(register_map.blocks)
    registers = select_registers(register_map, blocks=blocks, write_level=write_level)
    max_registers = int(options.get(CONF_MAX_REGISTERS, DEFAULT_MAX_REGISTERS))

    coordinators: dict[Tier, MbioCoordinator] = {}
    for tier, tier_regs in tier_registers(registers).items():
        coordinators[tier] = MbioCoordinator(
            hass,
            entry,
            client,
            tier,
            tier_regs,
            interval=_interval_for(options, tier),
            max_registers=max_registers,
            readable=register_map.addresses,
        )

    room_sensors = options.get(CONF_ROOM_SENSORS)
    runtime = WaterstageRuntime(
        gateway=gateway,
        client=client,
        register_map=register_map,
        registers=registers,
        coordinators=coordinators,
        discovery=DiscoveryResult(
            blocks={name: name in blocks for name in register_map.blocks},
            reasons=options.get(CONF_DISCOVERY_REASONS) or {},
            room_sensors=tuple(room_sensors or ()),
        ),
        write_level=write_level,
    )

    try:
        for coordinator in coordinators.values():
            await coordinator.async_config_entry_first_refresh()
    except Exception:
        await async_release_gateway(gateway)
        raise

    if room_sensors is None:
        # An entry created before room sensors were recorded, or one whose
        # discovery failed: fall back to what the first read just told us.
        room_sensors = find_room_sensors(register_map, [_all_values(runtime)])
        # Record what was actually used, not what the options happened to hold,
        # so the diagnostics dump explains the entity set it produced.
        runtime.discovery = replace(runtime.discovery, room_sensors=tuple(room_sensors))

    runtime.board_info = _board_info(runtime)
    runtime.controls = assign_controls(
        registers, write_level=write_level, room_sensors=room_sensors
    )
    entry.runtime_data = runtime

    _register_devices(hass, entry, runtime)

    link_coordinator = runtime.link_coordinator
    if link_coordinator is not None:
        entry.async_on_unload(
            link_coordinator.async_add_listener(async_link_watcher(runtime))
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # DESIGN.md section 5: a board that was just powered up answers 0 for a few
    # minutes, but the first read is what makes it fetch the real value from the
    # BSB bus.  Asking again shortly after start-up gets the answer without
    # holding up Home Assistant's own start-up.
    entry.async_create_background_task(
        hass, _async_second_read(entry, runtime), "waterstage warm-up read"
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: WaterstageConfigEntry) -> bool:
    """Stop polling and drop this entry's claim on the shared connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await async_release_gateway(entry.runtime_data.gateway)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: WaterstageConfigEntry) -> None:
    """Options changed -- rebuild the entity set and the polling tiers."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_second_read(
    entry: WaterstageConfigEntry, runtime: WaterstageRuntime
) -> None:
    """Refresh everything once more after the board has had time to catch up."""
    await asyncio.sleep(SETUP_SECOND_READ_DELAY)
    for coordinator in runtime.coordinators.values():
        await coordinator.async_request_refresh()
    runtime.board_info = _board_info(runtime)


def _interval_for(options: Mapping[str, Any], tier: Tier) -> int:
    """The configured interval, never faster than the tier's own refresh rate."""
    key = CONF_SCAN_INTERVAL_BY_TIER.get(tier)
    configured = tier.default_interval
    if key is not None:
        configured = int(options.get(key, tier.default_interval))
    floor = TIER_REFRESH_LIMIT[tier]
    if configured < floor:
        _LOGGER.debug(
            "Raising the %s interval from %ss to %ss: the board does not refresh "
            "from the BSB bus any faster",
            tier.value,
            configured,
            floor,
        )
        return floor
    return configured


def _all_values(runtime: WaterstageRuntime) -> dict[str, Any]:
    """Everything the tiers have read so far, in one mapping."""
    values: dict[str, Any] = {}
    for coordinator in runtime.coordinators.values():
        if coordinator.data:
            values.update(coordinator.data)
    return values


def _board_info(runtime: WaterstageRuntime) -> dict[int, int]:
    """Pick the identification registers out of whatever has been read."""
    info: dict[int, int] = {}
    for address in _BOARD_INFO_ADDRESSES:
        register = runtime.register_map.at(address)
        if register is None:
            continue
        decoded = runtime.decoded(register)
        if decoded is not None and isinstance(decoded.value, int):
            info[address] = decoded.value
    return info


def _register_devices(
    hass: HomeAssistant, entry: WaterstageConfigEntry, runtime: WaterstageRuntime
) -> None:
    """Create both devices up front so ``via_device`` always resolves."""
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, **heat_pump_device_info(entry, runtime)
    )
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, **board_device_info(entry, runtime)
    )
