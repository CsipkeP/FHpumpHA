"""Config and options flow.

The important behaviour here is refusal: the RS-485 gateway is shared, so
setup must recognise a device that is not an MBIO board and stop, without
writing anything anywhere.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fujitsu_waterstage.const import (
    CONF_BLOCKS,
    CONF_FUNCTION_CODE,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL_FAST,
    CONF_SLAVE_ID,
    CONF_WRITE_LEVEL,
    DOMAIN,
    WriteLevel,
)
from custom_components.fujitsu_waterstage.hub import (
    FUNCTION_READ_HOLDING,
    FUNCTION_READ_INPUT,
)

from .fake_board import FakeClient, FakeResponse, make_board
from .helpers import USER_INPUT, patch_board

pytestmark = pytest.mark.usefixtures("auto_enable_custom_integrations")


async def _run_user_flow(
    hass: HomeAssistant, client: FakeClient, user_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    with patch_board(client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input or USER_INPUT
        )
        await hass.async_block_till_done()
    return result


class TestUserStep:
    async def test_creates_an_entry(self, hass: HomeAssistant) -> None:
        result = await _run_user_flow(hass, make_board())

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Waterstage"
        assert result["data"] == {
            CONF_HOST: "192.168.1.37",
            CONF_PORT: 502,
            CONF_SLAVE_ID: 3,
            CONF_FUNCTION_CODE: FUNCTION_READ_HOLDING,
        }

    async def test_stores_the_discovered_blocks(self, hass: HomeAssistant) -> None:
        result = await _run_user_flow(hass, make_board())
        options = result["options"]

        assert options[CONF_WRITE_LEVEL] == WriteLevel.BASIC.value
        assert "heat_pump" in options[CONF_BLOCKS]
        # The default board has no pool and no second heating circuit.
        assert "swimming_pool" not in options[CONF_BLOCKS]
        assert "heating_circuit_2" not in options[CONF_BLOCKS]

    async def test_unique_id_from_the_serial_number(self, hass: HomeAssistant) -> None:
        await _run_user_flow(hass, make_board())
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == "17040042"  # registers 9902 and 9903

    async def test_unique_id_falls_back_to_the_address(
        self, hass: HomeAssistant
    ) -> None:
        board = make_board()
        board.words[9902] = 0
        board.words[9903] = 0
        await _run_user_flow(hass, board)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == "192.168.1.37:502:3"

    async def test_falls_back_to_function_code_4(self, hass: HomeAssistant) -> None:
        """Both codes reach the same registers, but not every gateway allows both."""

        class InputOnly(FakeClient):
            """A gateway that rejects 0x03 but answers 0x04."""

            async def read_holding_registers(self, address: int, **kwargs: Any):
                self.calls.append(("read03", address, kwargs.get("count", 1)))
                return FakeResponse(error=True)

            async def read_input_registers(
                self, address: int, *, count: int = 1, **kwargs: Any
            ):
                self.calls.append(("read04", address, count))
                return FakeResponse(
                    [self.words.get(address + i, 0) for i in range(count)]
                )

        board = InputOnly(words=make_board().words)
        result = await _run_user_flow(hass, board)
        assert result["data"][CONF_FUNCTION_CODE] == FUNCTION_READ_INPUT


class TestUserStepRefusals:
    async def test_wrong_product_code_is_reported_and_nothing_is_written(
        self, hass: HomeAssistant
    ) -> None:
        """Another device on the same gateway must be left completely alone."""
        board = make_board()
        board.words[9900] = 0x1234

        result = await _run_user_flow(hass, board)

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "unknown_device"}
        assert not [call for call in board.calls if call[0].startswith("write")]
        # A single identification read, then it stops.  No writes, no probing
        # of any other address on a device that is not ours.
        assert [call[1] for call in board.calls] == [9900]

    async def test_no_answer(self, hass: HomeAssistant) -> None:
        result = await _run_user_flow(hass, FakeClient(fail_reads=10_000))
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_unexpected_error(self, hass: HomeAssistant) -> None:
        with patch(
            "custom_components.fujitsu_waterstage.config_flow.async_identify",
            side_effect=RuntimeError("boom"),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )
        assert result["errors"] == {"base": "unknown"}

    async def test_the_form_comes_back_filled_in(self, hass: HomeAssistant) -> None:
        """Retyping the address after a rejection would be annoying."""
        board = make_board()
        board.words[9900] = 0x1234
        result = await _run_user_flow(hass, board)

        suggestions = {
            str(key): key.description["suggested_value"]
            for key in result["data_schema"].schema
            if key.description
        }
        assert suggestions[CONF_HOST] == "192.168.1.37"
        assert suggestions[CONF_SLAVE_ID] == 3

    async def test_the_same_board_cannot_be_added_twice(
        self, hass: HomeAssistant
    ) -> None:
        await _run_user_flow(hass, make_board())
        result = await _run_user_flow(hass, make_board())
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestOptionsFlow:
    async def _entry(self, hass: HomeAssistant) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_HOST: "192.168.1.37",
                CONF_PORT: 502,
                CONF_SLAVE_ID: 3,
                CONF_FUNCTION_CODE: FUNCTION_READ_HOLDING,
            },
            options={CONF_BLOCKS: ["heat_pump", "interface"]},
            title="Waterstage",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_shows_the_form(self, hass: HomeAssistant) -> None:
        entry = await self._entry(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

    async def test_stores_the_choices(self, hass: HomeAssistant) -> None:
        entry = await self._entry(hass)
        with patch_board(make_board()):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {
                    CONF_BLOCKS: ["heat_pump", "swimming_pool"],
                    CONF_WRITE_LEVEL: WriteLevel.ADVANCED.value,
                    CONF_SCAN_INTERVAL_FAST: 45,
                    "scan_interval_normal": 120,
                    "scan_interval_slow": 300,
                    "inter_request_delay_ms": 50,
                    "timeout_s": 5,
                    CONF_RETRIES: 3,
                    "max_registers_per_read": 120,
                },
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_WRITE_LEVEL] == WriteLevel.ADVANCED.value
        assert entry.options[CONF_SCAN_INTERVAL_FAST] == 45
        # The blocks that always exist are added back in whatever the user picked.
        assert "swimming_pool" in entry.options[CONF_BLOCKS]
        assert "interface" in entry.options[CONF_BLOCKS]
        assert "faults" in entry.options[CONF_BLOCKS]

    async def test_the_interface_block_is_not_offered(
        self, hass: HomeAssistant
    ) -> None:
        """Register 0 lives there and everything else's availability depends on it."""
        entry = await self._entry(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        schema = result["data_schema"].schema
        blocks_field = next(key for key in schema if str(key) == CONF_BLOCKS)
        assert "interface" not in schema[blocks_field].config["options"]
