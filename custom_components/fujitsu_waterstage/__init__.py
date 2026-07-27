"""Fujitsu Waterstage heat pump over an FWS-MBIO-002 Modbus interface board."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_BLOCKS,
    CONF_FUNCTION_CODE,
    CONF_INTER_REQUEST_DELAY_MS,
    CONF_MAX_REGISTERS,
    CONF_RETRIES,
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
from .discovery import DiscoveryResult, select_registers, tier_registers
from .entity import board_device_info, heat_pump_device_info
from .hub import (
    FUNCTION_READ_HOLDING,
    MbioClient,
    async_get_gateway,
    async_release_gateway,
)
from .registers import load_register_map

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

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
        timeout=float(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        retries=int(options.get(CONF_RETRIES, DEFAULT_RETRIES)),
        inter_request_delay=int(
            options.get(CONF_INTER_REQUEST_DELAY_MS, DEFAULT_INTER_REQUEST_DELAY_MS)
        )
        / 1000,
    )
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
        )

    runtime = WaterstageRuntime(
        gateway=gateway,
        client=client,
        register_map=register_map,
        registers=registers,
        coordinators=coordinators,
        discovery=DiscoveryResult(blocks={name: name in blocks for name in register_map.blocks}),
        write_level=write_level,
    )

    try:
        for coordinator in coordinators.values():
            await coordinator.async_config_entry_first_refresh()
    except Exception:
        await async_release_gateway(gateway)
        raise

    runtime.board_info = _board_info(runtime)
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
