"""Setup, entities and the availability rules, against a fake board.

The real gateway, coordinators, read groups and codec are all in play; only the
socket is a fake.  What is being checked here is mostly *when a value must not
be shown*: a disabled data point, a dead BSB link and a board that is still
warming up all have to end up as unavailable or unknown rather than as 0.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.fujitsu_waterstage.const import (
    ATTR_CODE,
    DOMAIN,
    Tier,
    WriteLevel,
)
from custom_components.fujitsu_waterstage.discovery import select_registers
from custom_components.fujitsu_waterstage.entity import (
    board_device_id,
    heat_pump_device_id,
)
from custom_components.fujitsu_waterstage.sensor import is_two_state

from .fake_board import FakeClient, make_board
from .helpers import poll, setup_integration, state_of

pytestmark = pytest.mark.usefixtures("auto_enable_custom_integrations")


class TestSetup:
    async def test_entry_loads(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        assert entry.state is ConfigEntryState.LOADED

    async def test_every_selected_register_becomes_one_entity(
        self, hass: HomeAssistant, register_map
    ) -> None:
        entry, _ = await setup_integration(hass)
        expected = select_registers(register_map, write_level=WriteLevel.BASIC)

        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert len(entities) == len(expected)
        assert {entity.domain for entity in entities} == {"sensor", "binary_sensor"}

    async def test_expert_registers_have_no_entity(
        self, hass: HomeAssistant, register_map
    ) -> None:
        """38, 39, 460, 461 and 9907 switch hardware or restart it."""
        entry, _ = await setup_integration(hass)
        registry = er.async_get(hass)
        for address in (38, 39, 460, 461, 9907):
            key = register_map.at(address).key
            for platform in ("sensor", "binary_sensor"):
                assert (
                    registry.async_get_entity_id(
                        platform, DOMAIN, f"{entry.entry_id}_{key}"
                    )
                    is None
                ), address

    async def test_disabled_blocks_have_no_entities(
        self, hass: HomeAssistant, register_map
    ) -> None:
        entry, _ = await setup_integration(hass, blocks=["interface", "heat_pump"])
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert len(entities) == len(
            select_registers(register_map, blocks=["interface", "heat_pump"])
        )

    async def test_four_tiers_with_sane_intervals(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        coordinators = entry.runtime_data.coordinators
        assert set(coordinators) == set(Tier)
        for tier, coordinator in coordinators.items():
            assert coordinator.update_interval.total_seconds() == tier.default_interval

    async def test_a_configured_interval_cannot_beat_the_board(
        self, hass: HomeAssistant
    ) -> None:
        """The MBIO refreshes the fast values every 30 s at best."""
        entry, _ = await setup_integration(hass, options={"scan_interval_fast": 10})
        fast = entry.runtime_data.coordinators[Tier.FAST]
        assert fast.update_interval.total_seconds() == 30

    async def test_unload(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED

    async def test_setup_fails_when_the_board_is_silent(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(
            hass, FakeClient(fail_reads=10_000), expect_success=False
        )
        assert entry.state is ConfigEntryState.SETUP_RETRY


class TestDevices:
    async def test_two_devices_linked_by_via_device(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        registry = dr.async_get(hass)

        heat_pump = registry.async_get_device(identifiers={heat_pump_device_id(entry)})
        board = registry.async_get_device(identifiers={board_device_id(entry)})

        assert heat_pump is not None
        assert board is not None
        assert board.via_device_id == heat_pump.id
        assert heat_pump.manufacturer == "Fujitsu"
        assert board.manufacturer == "ACITECH Solutions"
        assert board.model == "FWS-MBIO-002"

    async def test_versions_come_from_the_board(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        registry = dr.async_get(hass)
        heat_pump = registry.async_get_device(identifiers={heat_pump_device_id(entry)})
        board = registry.async_get_device(identifiers={board_device_id(entry)})

        assert heat_pump.sw_version == "V8.5"  # register 440 reports 85
        assert board.serial_number == "17040042"  # registers 9902 and 9903

    async def test_board_local_entities_belong_to_the_board(
        self, hass: HomeAssistant
    ) -> None:
        """Register 13 is measured by the interface, not by the controller."""
        entry, _ = await setup_integration(hass)
        registry = er.async_get(hass)
        devices = dr.async_get(hass)
        board = devices.async_get_device(identifiers={board_device_id(entry)})

        entity = registry.async_get(
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_heat_pump_heat_exchanger_internal_temperature"
            )
        )
        assert entity.device_id == board.id

        uptime = registry.async_get(
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_interface_interface_uptime"
            )
        )
        assert uptime.device_id == board.id


class TestSensorValues:
    async def test_temperature(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "heat_pump_return_temperature")
        assert state.state == "25.0"
        assert state.attributes[ATTR_DEVICE_CLASS] == "temperature"
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.CELSIUS

    async def test_negative_temperature(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        assert (
            state_of(hass, entry, "heat_pump_outside_temperature_actual_value").state
            == "-10.1"
        )

    async def test_scaled_uint16(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(
            hass, entry, "heat_pump_current_starts_compressor_1_per_hours_run"
        )
        assert state.state == "3.5"

    async def test_runtime_counter_in_hours(self, hass: HomeAssistant) -> None:
        """74565 seconds, published as hours with a total_increasing class."""
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "heat_pump_compressor_1_runtime")
        assert float(state.state) == pytest.approx(74565 / 3600)
        assert state.attributes[ATTR_DEVICE_CLASS] == "duration"
        assert state.attributes["state_class"] == "total_increasing"
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "h"

    async def test_plain_counter_has_no_unit(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "heat_pump_compressor_1_start_counter")
        assert state.state == "0"
        assert ATTR_UNIT_OF_MEASUREMENT not in state.attributes

    async def test_status_code_becomes_text(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "heat_pump_heat_pump_status")
        assert state.state == "Heating mode"
        assert state.attributes[ATTR_CODE] == 137

    async def test_error_code_becomes_text(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "faults_fault_history_1_error_code")
        assert state.state == "Maintenance message"
        assert state.attributes[ATTR_CODE] == 105

    async def test_an_undocumented_code_is_unknown_but_still_visible(
        self, hass: HomeAssistant
    ) -> None:
        """The board may report a status the manual does not list."""
        entry, client = await setup_integration(hass)
        client.words[1] = 60000
        await poll(hass, entry)

        state = state_of(hass, entry, "heat_pump_heat_pump_status")
        assert state.state == STATE_UNKNOWN
        assert state.attributes[ATTR_CODE] == 60000

    async def test_inline_options_become_an_enum(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "dhw_dhw_operating_mode")
        assert state.state == "On"
        assert state.attributes[ATTR_DEVICE_CLASS] == "enum"
        assert state.attributes["options"] == ["Off", "On", "Eco"]

    async def test_timestamp(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "faults_fault_history_1_date_time")
        assert state.attributes[ATTR_DEVICE_CLASS] == "timestamp"
        # The controller keeps local wall clock time; Home Assistant publishes
        # timestamps in UTC, so compare the instants rather than the strings.
        assert dt_util.parse_datetime(state.state) == datetime(
            2023, 4, 11, 11, 20, tzinfo=dt_util.DEFAULT_TIME_ZONE
        )

    async def test_an_empty_timestamp_slot_is_unknown(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass)
        assert (
            state_of(hass, entry, "faults_fault_history_2_date_time").state
            == STATE_UNKNOWN
        )

    async def test_settings_are_diagnostic(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        registry = er.async_get(hass)
        setpoint = registry.async_get(
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_dhw_dhw_nominal_temperature_setpoint"
            )
        )
        assert setpoint.entity_category is EntityCategory.DIAGNOSTIC

        measurement = registry.async_get(
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_heat_pump_return_temperature"
            )
        )
        assert measurement.entity_category is None


class TestBinarySensors:
    async def test_on_off_uses_255(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        assert (
            state_of(hass, entry, "heat_pump_compressor_1_status", "binary_sensor").state
            == STATE_ON
        )
        assert (
            state_of(
                hass, entry, "heat_pump_condenser_pump_status", "binary_sensor"
            ).state
            == STATE_OFF
        )

    async def test_room_thermostat_uses_one(self, hass: HomeAssistant) -> None:
        """The documented exception to the 0/255 rule."""
        entry, _ = await setup_integration(hass)
        state = state_of(
            hass, entry, "heating_circuit_1_room_thermostat_1", "binary_sensor"
        )
        assert state.state == STATE_ON
        assert state.attributes[ATTR_CODE] == 1

    async def test_link_status_is_a_connectivity_sensor(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(
            hass, entry, "interface_communication_status", "binary_sensor"
        )
        assert state.state == STATE_ON
        assert state.attributes[ATTR_DEVICE_CLASS] == "connectivity"

    async def test_two_option_registers_that_are_not_on_off_stay_sensors(
        self, hass: HomeAssistant, register_map
    ) -> None:
        """440 has two options (V8.5 / V8.9) but neither of them is 0."""
        assert not is_two_state(register_map.at(440))
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, "faults_rvs21_software_version")
        assert state.state == "V8.5 (RVS21.831/127 Serie F)"

    async def test_an_undocumented_state_is_unknown(
        self, hass: HomeAssistant, caplog
    ) -> None:
        entry, client = await setup_integration(hass, make_board(**{}))
        client.words[2] = 42  # neither 0 nor 255
        await poll(hass, entry)

        state = state_of(hass, entry, "heat_pump_compressor_1_status", "binary_sensor")
        assert state.state == STATE_UNKNOWN
        assert "not one of its documented states" in caplog.text


class TestAvailability:
    async def test_a_disabled_data_point_is_unavailable_not_zero(
        self, hass: HomeAssistant
    ) -> None:
        """Register 13 comes back with the /O disable bit set."""
        entry, _ = await setup_integration(hass)
        state = state_of(
            hass, entry, "heat_pump_heat_exchanger_internal_temperature"
        )
        assert state.state == STATE_UNAVAILABLE

    async def test_a_bsb_outage_hides_the_controller_but_not_the_board(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        client.words[0] = 0  # BSB communication error
        await poll(hass, entry)

        # Everything the RVS21 produced is stale, whatever tier it is in.
        assert (
            state_of(hass, entry, "heat_pump_return_temperature").state
            == STATE_UNAVAILABLE
        )
        assert (
            state_of(hass, entry, "dhw_dhw_nominal_temperature_setpoint").state
            == STATE_UNAVAILABLE
        )
        assert (
            state_of(hass, entry, "heat_pump_heat_pump_status").state
            == STATE_UNAVAILABLE
        )

        # The board's own registers are still perfectly valid.
        assert state_of(hass, entry, "interface_interface_uptime").state != STATE_UNAVAILABLE
        assert (
            state_of(hass, entry, "interface_communication_status", "binary_sensor").state
            == STATE_OFF
        )

    async def test_the_link_recovers(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        client.words[0] = 0
        await poll(hass, entry)
        assert (
            state_of(hass, entry, "heat_pump_return_temperature").state
            == STATE_UNAVAILABLE
        )

        client.words[0] = 1
        await poll(hass, entry)
        assert state_of(hass, entry, "heat_pump_return_temperature").state == "25.0"

    async def test_a_slow_tier_reacts_to_the_link_without_waiting_for_its_poll(
        self, hass: HomeAssistant
    ) -> None:
        """Availability changes the moment register 0 does, not five minutes later."""
        entry, client = await setup_integration(hass)
        slow_groups = {
            group.start for group in entry.runtime_data.coordinators[Tier.SLOW].groups
        }
        before = len(client.calls)

        client.words[0] = 0
        await poll(hass, entry, Tier.FAST)  # only the tier that owns register 0

        polled = {call[1] for call in client.calls[before:]}
        assert not polled & slow_groups  # the slow tier did not read anything
        assert (
            state_of(hass, entry, "dhw_dhw_nominal_temperature_setpoint").state
            == STATE_UNAVAILABLE
        )

    async def test_a_dead_bus_is_a_different_failure(
        self, hass: HomeAssistant
    ) -> None:
        """No Modbus answer at all takes the board's own entities down too."""
        entry, client = await setup_integration(hass)
        client.fail_reads = 10_000
        await poll(hass, entry)

        assert (
            state_of(hass, entry, "interface_interface_uptime").state
            == STATE_UNAVAILABLE
        )
        assert (
            state_of(hass, entry, "heat_pump_return_temperature").state
            == STATE_UNAVAILABLE
        )

    async def test_one_rejected_group_does_not_blind_the_whole_tier(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        client.rejected = {120}  # the HC1 status group
        await poll(hass, entry)

        assert state_of(hass, entry, "heat_pump_return_temperature").state == "25.0"
        assert (
            state_of(hass, entry, "heating_circuit_1_heating_circuit_1_status").state
            == STATE_UNAVAILABLE
        )


class TestWarmUp:
    async def test_a_zero_temperature_is_unknown_while_the_board_warms_up(
        self, hass: HomeAssistant
    ) -> None:
        """A freshly powered board answers 0 before it has queried the BSB bus."""
        entry, _ = await setup_integration(hass)
        assert (
            state_of(hass, entry, "heat_pump_condenser_temperature_differential").state
            == STATE_UNKNOWN
        )

    async def test_a_real_zero_is_published_once_the_warm_up_is_over(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        for coordinator in entry.runtime_data.coordinators.values():
            coordinator._warmup_until = 0
        await poll(hass, entry)

        assert (
            state_of(hass, entry, "heat_pump_condenser_temperature_differential").state
            == "0.0"
        )

    async def test_non_temperature_zeros_are_never_suppressed(
        self, hass: HomeAssistant
    ) -> None:
        """0 active errors is a fact, not a missing reading."""
        entry, _ = await setup_integration(hass)
        assert state_of(hass, entry, "faults_count_of_active_errors").state == "0"

    async def test_a_register_that_reported_a_value_keeps_reporting_zero(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        assert state_of(hass, entry, "heat_pump_return_temperature").state == "25.0"

        client.words[7] = 0
        await poll(hass, entry)
        assert state_of(hass, entry, "heat_pump_return_temperature").state == "0.0"
