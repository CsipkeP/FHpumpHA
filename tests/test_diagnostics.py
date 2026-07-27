"""The diagnostics dump.

A bug report has to be enough on its own, so these tests check that the two
things which actually explain a fault are in there: the raw words next to what
they decoded to, and the BSB error counters.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder

from custom_components.fujitsu_waterstage.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .fake_board import make_board
from .helpers import poll, setup_integration

pytestmark = pytest.mark.usefixtures("auto_enable_custom_integrations")


async def _dump(hass: HomeAssistant, **kwargs) -> dict:
    entry, client = await setup_integration(hass, **kwargs)
    return await async_get_config_entry_diagnostics(hass, entry), entry, client


class TestRedaction:
    async def test_the_gateway_address_is_hidden(self, hass: HomeAssistant) -> None:
        dump, _, _ = await _dump(hass)
        assert dump["entry"]["data"]["host"] == REDACTED
        assert "192.168.1.37" not in json.dumps(dump, cls=ExtendedJSONEncoder)

    async def test_the_address_is_hidden_inside_the_unique_id_too(
        self, hass: HomeAssistant
    ) -> None:
        """Without a serial number the unique id is host:port:slave."""
        board = make_board()
        board.words[9902] = 0
        board.words[9903] = 0
        entry, _ = await setup_integration(hass, board)
        hass.config_entries.async_update_entry(entry, unique_id="192.168.1.37:502:3")

        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["entry"]["unique_id"] == f"{REDACTED}:502:3"

    async def test_the_board_serial_is_kept(self, hass: HomeAssistant) -> None:
        """It identifies the hardware, not the household, and a report needs it."""
        dump, _, _ = await _dump(hass)
        assert dump["board"]["serial_number"] == "17040042"


class TestBoard:
    async def test_identification(self, hass: HomeAssistant) -> None:
        dump, _, _ = await _dump(hass)
        board = dump["board"]
        assert board["identification"]["9900 Product code"] == 0x0401
        assert board["rvs21_software_version"] == "V8.5"
        assert board["read_function_code"] == "0x3"
        assert board["slave_id"] == 3
        assert board["connected"] is True


class TestCommunication:
    async def test_the_link_status_is_resolved_to_text(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        link = dump["communication"]["bsb_link"]
        assert link == {"raw": 1, "text": "OK", "ok": True}

    async def test_a_broken_link_is_visible(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        client.words[0] = 0
        await poll(hass, entry)

        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["communication"]["bsb_link"] == {
            "raw": 0,
            "text": "BSB communication error",
            "ok": False,
        }

    async def test_the_interface_error_code_is_resolved(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        client.words[9912] = 33
        await poll(hass, entry)

        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["communication"]["interface_error"]["text"] == (
            "BSB communication error between the RVS21 board and the interface"
        )

    async def test_every_error_counter_is_reported(
        self, hass: HomeAssistant
    ) -> None:
        """DESIGN.md 12 -- if these grow, the wiring is the problem."""
        entry, client = await setup_integration(hass)
        client.words[9916] = 12  # BSB framing errors
        await poll(hass, entry)

        dump = await async_get_config_entry_diagnostics(hass, entry)
        communication = dump["communication"]
        assert len(communication["modbus_errors"]) == 3
        assert len(communication["bsb_errors"]) == 4
        assert communication["bsb_errors"]["9916 BSB framing error count"] == 12
        assert set(communication["bsb_utilisation"]) == {
            "9920 BSB bus current utilisation",
            "9921 BSB bus maximum utilisation",
        }


class TestDiscovery:
    async def test_excluded_blocks_come_with_a_reason(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(
            hass,
            blocks=["interface", "heat_pump", "dhw", "heating_circuit_1", "faults"],
            options={
                "discovery_reasons": {
                    "swimming_pool": "every indicator register read 0 or disabled"
                }
            },
        )
        dump = await async_get_config_entry_diagnostics(hass, entry)

        excluded = dump["discovery"]["excluded_blocks"]
        assert "0 or disabled" in excluded["swimming_pool"]
        # A block with no stored reason still says why it is off.
        assert excluded["solar"] == "not enabled in the options"

    async def test_the_write_level_and_room_sensors_are_recorded(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        assert dump["discovery"]["write_level"] == "basic"
        assert dump["discovery"]["room_sensors"] == ["heating_circuit_1"]


class TestTiersAndRegisters:
    async def test_every_group_carries_its_last_raw_answer(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        for tier in ("fast", "normal", "slow", "static"):
            for group in dump["tiers"][tier]["groups"]:
                assert len(group["raw"]) == group["count"], (tier, group["start"])

    async def test_a_failed_group_is_named(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        client.rejected = {120}
        await poll(hass, entry)

        dump = await async_get_config_entry_diagnostics(hass, entry)
        failures = {
            label
            for tier in dump["tiers"].values()
            for label in tier["failed_groups"]
        }
        assert any(label.startswith("120..") for label in failures)

    async def test_raw_words_sit_next_to_the_decoded_value(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        entry = dump["registers"]["heat_pump_return_temperature"]
        assert entry["raw"] == ["00FA"]  # 250
        assert entry["value"] == 25.0
        assert entry["disabled"] is False
        assert entry["tier"] == "fast"
        assert entry["source"] == "rvs21"

    async def test_a_disabled_data_point_shows_both_halves(
        self, hass: HomeAssistant
    ) -> None:
        """The whole point: 0x4065 decodes to 10.1 °C *and* is disabled."""
        dump, _, _ = await _dump(hass)
        entry = dump["registers"][
            "heat_pump_heat_exchanger_internal_temperature"
        ]
        assert entry["raw"] == ["4065"]
        assert entry["value"] == 10.1
        assert entry["disabled"] is True
        assert entry["source"] == "board"

    async def test_a_two_register_value_shows_both_words(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        entry = dump["registers"]["heat_pump_compressor_1_runtime"]
        assert entry["raw"] == ["0001", "2345"]
        assert entry["value"] == 0x00012345

    async def test_coded_registers_carry_their_text(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        assert dump["registers"]["heat_pump_heat_pump_status"]["text"] == "Heating mode"

    async def test_the_controls_of_each_register_are_listed(
        self, hass: HomeAssistant
    ) -> None:
        dump, _, _ = await _dump(hass)
        registers = dump["registers"]
        assert registers["dhw_dhw_operating_mode"]["controls"] == ["water_heater"]
        assert registers["heat_pump_return_temperature"]["controls"] == ["sensor"]


class TestSerialisation:
    async def test_the_whole_dump_is_json_serialisable(
        self, hass: HomeAssistant
    ) -> None:
        """It has to survive the download button, timestamps and all."""
        dump, _, _ = await _dump(hass)
        text = json.dumps(dump, cls=ExtendedJSONEncoder)
        assert json.loads(text)["registers"]["faults_fault_history_1_date_time"][
            "value"
        ].startswith("2023-04-11")

    async def test_an_unloaded_entry_says_so_instead_of_crashing(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["error"] == "the config entry is not loaded"
        assert dump["entry"]["data"]["host"] == REDACTED
