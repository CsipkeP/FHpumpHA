"""Shared helpers for the Home Assistant level tests."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fujitsu_waterstage.const import (
    CONF_BLOCKS,
    CONF_FUNCTION_CODE,
    CONF_ROOM_SENSORS,
    CONF_SLAVE_ID,
    CONF_WRITE_LEVEL,
    DOMAIN,
    WriteLevel,
)
from custom_components.fujitsu_waterstage.hub import FUNCTION_READ_HOLDING

from .fake_board import FakeClient, make_board

USER_INPUT: dict[str, Any] = {
    CONF_HOST: "192.168.1.37",
    CONF_PORT: 502,
    CONF_SLAVE_ID: 3,
    CONF_NAME: "Waterstage",
}

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: "192.168.1.37",
    CONF_PORT: 502,
    CONF_SLAVE_ID: 3,
    CONF_FUNCTION_CODE: FUNCTION_READ_HOLDING,
}


@contextlib.contextmanager
def patch_board(
    client: FakeClient, *, reread_delay: float = 0
) -> Iterator[FakeClient]:
    """Put a fake board behind the real gateway, and skip every test delay.

    The gateway, its lock, the retry policy and the read groups are all the real
    ones -- only the socket is replaced.
    """
    with (
        patch(
            "custom_components.fujitsu_waterstage.hub.AsyncModbusTcpClient",
            return_value=client,
        ),
        patch("custom_components.fujitsu_waterstage.SETUP_SECOND_READ_DELAY", 0),
        patch("custom_components.fujitsu_waterstage.config_flow._DISCOVERY_DELAY", 0),
        patch(
            "custom_components.fujitsu_waterstage.coordinator.WRITE_REREAD_DELAY",
            reread_delay,
        ),
    ):
        yield client


async def setup_integration(
    hass: HomeAssistant,
    client: FakeClient | None = None,
    *,
    blocks: list[str] | None = None,
    write_level: WriteLevel = WriteLevel.BASIC,
    room_sensors: list[str] | None = None,
    options: dict[str, Any] | None = None,
    expect_success: bool = True,
) -> tuple[MockConfigEntry, FakeClient]:
    """Set the integration up against a fake board and wait for it to settle."""
    client = client or make_board()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Waterstage",
        unique_id="17040042",
        data=ENTRY_DATA,
        options={
            CONF_BLOCKS: blocks,
            CONF_WRITE_LEVEL: write_level.value,
            # Left out entirely by default, so setup falls back to deciding
            # from what it just read.
            **({} if room_sensors is None else {CONF_ROOM_SENSORS: room_sensors}),
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    with patch_board(client):
        loaded = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert loaded is expect_success
    return entry, client


async def poll(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *tiers: Any,
) -> None:
    """Run one polling cycle.

    The coordinators are refreshed directly rather than by moving the test
    clock: what these tests are about is what the entities do with the answer,
    and Home Assistant's own scheduling is covered by asserting the intervals.
    Pass tiers to refresh only some of them.
    """
    coordinators = entry.runtime_data.coordinators
    wanted = [coordinators[tier] for tier in tiers] if tiers else coordinators.values()
    for coordinator in wanted:
        await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id_of(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    register_key: str,
    platform: str = "sensor",
) -> str:
    """The entity id for a register key, or a clear failure."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{register_key}"
    )
    assert entity_id is not None, f"no {platform} entity for {register_key}"
    return entity_id


def state_of(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    register_key: str,
    platform: str = "sensor",
) -> Any:
    """Look a state up by register key rather than by guessed entity id."""
    return hass.states.get(entity_id_of(hass, entry, register_key, platform))
