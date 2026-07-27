"""What each write level is allowed to write, and what entity it becomes.

This is the safety-critical rule of the whole integration: ``R/W`` in the
register map is a statement about the hardware, not permission to put a control
on the dashboard.  It is tested here without Home Assistant, directly against
the real register map.
"""

from __future__ import annotations

import pytest
from fujitsu_waterstage.const import (
    BASIC_WRITE_ADDRESSES,
    EXPERT_ACTIONS,
    RESET_BY_WRITE_ADDRESSES,
    Control,
    WriteLevel,
)
from fujitsu_waterstage.discovery import (
    assign_controls,
    is_two_state,
    select_registers,
    write_allowed,
)
from fujitsu_waterstage.registers import RegisterMap

#: The seven data points of DESIGN.md 10.1, spelled out again on purpose.
BASIC_SEVEN = (40, 41, 100, 101, 102, 106, 107)
EXPERT_ONLY = (38, 39, 460, 461, 9907)


def _controls(register_map: RegisterMap, level: WriteLevel, **kwargs):
    selected = select_registers(register_map, write_level=level)
    return selected, assign_controls(selected, write_level=level, **kwargs)


class TestBasicLevel:
    def test_exactly_seven_registers_are_writable(
        self, register_map: RegisterMap
    ) -> None:
        writable = [
            register.address
            for register in select_registers(register_map)
            if write_allowed(register, WriteLevel.BASIC)
        ]
        assert sorted(writable) == sorted(BASIC_SEVEN)
        assert set(BASIC_SEVEN) == BASIC_WRITE_ADDRESSES

    def test_every_other_rw_register_stays_read_only(
        self, register_map: RegisterMap
    ) -> None:
        """The point of the level: R/W alone earns nothing."""
        selected, controls = _controls(register_map, WriteLevel.BASIC)
        writable_kinds = {Control.NUMBER, Control.SELECT, Control.BUTTON}
        for register in selected:
            if register.address in BASIC_SEVEN:
                continue
            assert not controls[register.key] & writable_kinds, register.key

    def test_there_are_plenty_of_read_only_rw_registers(
        self, register_map: RegisterMap
    ) -> None:
        """Guard against the previous test passing because nothing is R/W."""
        selected, _ = _controls(register_map, WriteLevel.BASIC)
        rw = [r for r in selected if r.writable and r.address not in BASIC_SEVEN]
        assert len(rw) > 50

    def test_no_reset_buttons(self, register_map: RegisterMap) -> None:
        _, controls = _controls(register_map, WriteLevel.BASIC)
        assert not [kinds for kinds in controls.values() if Control.BUTTON in kinds]

    def test_the_three_numbers(self, register_map: RegisterMap) -> None:
        """102, 106 and 107; the other four are climate and water heater."""
        selected, controls = _controls(
            register_map, WriteLevel.BASIC, room_sensors=["heating_circuit_1"]
        )
        numbers = [
            register.address
            for register in selected
            if Control.NUMBER in controls[register.key]
        ]
        assert sorted(numbers) == [102, 106, 107]

    def test_composite_entities_claim_the_other_four(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(
            register_map, WriteLevel.BASIC, room_sensors=["heating_circuit_1"]
        )
        by_address = {r.address: controls[r.key] for r in selected}
        assert by_address[100] == {Control.CLIMATE}
        assert by_address[101] == {Control.CLIMATE}
        assert by_address[40] == {Control.WATER_HEATER}
        assert by_address[41] == {Control.WATER_HEATER}

    def test_no_second_control_for_a_claimed_register(
        self, register_map: RegisterMap
    ) -> None:
        """A register never gets both a climate entity and a number."""
        _, controls = _controls(
            register_map, WriteLevel.BASIC, room_sensors=["heating_circuit_1"]
        )
        for key, kinds in controls.items():
            if kinds & {Control.CLIMATE, Control.WATER_HEATER}:
                assert len(kinds) == 1, key


class TestExpertRegisters:
    @pytest.mark.parametrize("level", [WriteLevel.BASIC, WriteLevel.ADVANCED])
    def test_not_even_created_below_expert(
        self, register_map: RegisterMap, level: WriteLevel
    ) -> None:
        """38, 39, 460, 461 and 9907 switch hardware; they get no entity at all."""
        selected = select_registers(register_map, write_level=level)
        assert not {r.address for r in selected} & set(EXPERT_ONLY)

    def test_expert_level_creates_them(self, register_map: RegisterMap) -> None:
        selected, controls = _controls(register_map, WriteLevel.EXPERT)
        by_address = {r.address: controls[r.key] for r in selected}
        assert set(EXPERT_ONLY) <= set(by_address)

    def test_actions_become_buttons_and_keep_their_sensor(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(register_map, WriteLevel.EXPERT)
        by_address = {r.address: controls[r.key] for r in selected}
        for address in EXPERT_ACTIONS:
            assert Control.BUTTON in by_address[address], address

    def test_the_bounded_tests_become_numbers(self, register_map: RegisterMap) -> None:
        selected, controls = _controls(register_map, WriteLevel.EXPERT)
        by_address = {r.address: controls[r.key] for r in selected}
        assert by_address[460] == {Control.NUMBER}  # relay test
        assert by_address[461] == {Control.NUMBER}  # output test UX2


class TestAdvancedLevel:
    def test_every_rw_register_becomes_a_control(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(
            register_map, WriteLevel.ADVANCED, room_sensors=["heating_circuit_1"]
        )
        writable_kinds = {
            Control.NUMBER,
            Control.SELECT,
            Control.BUTTON,
            Control.CLIMATE,
            Control.WATER_HEATER,
        }
        for register in selected:
            if register.writable:
                assert controls[register.key] & writable_kinds, register.key

    def test_coded_registers_become_selects(self, register_map: RegisterMap) -> None:
        selected, controls = _controls(register_map, WriteLevel.ADVANCED)
        by_address = {r.address: controls[r.key] for r in selected}
        assert by_address[43] == {Control.SELECT}  # DHW release
        assert by_address[46] == {Control.SELECT}  # legionella weekday
        assert by_address[143] == {Control.SELECT}  # cooling release, codes 1 and 2

    def test_reset_counters_keep_their_sensor(self, register_map: RegisterMap) -> None:
        """A runtime is worth reading whether or not you may clear it."""
        selected, controls = _controls(register_map, WriteLevel.ADVANCED)
        by_address = {r.address: controls[r.key] for r in selected}
        assert by_address[24] == {Control.BUTTON, Control.SENSOR}  # compressor runtime
        assert by_address[18] == {Control.BUTTON, Control.SENSOR}  # outside minimum

    def test_interface_counters_are_buttons_not_numbers(
        self, register_map: RegisterMap
    ) -> None:
        """The map calls them R/W, but a number box for a CRC error count is nonsense."""
        selected, controls = _controls(register_map, WriteLevel.ADVANCED)
        by_address = {r.address: controls[r.key] for r in selected}
        for address in RESET_BY_WRITE_ADDRESSES:
            assert Control.BUTTON in by_address[address], address
        assert Control.BUTTON not in by_address[9920]  # read-only, stays a sensor

    def test_hc2_climate_needs_advanced(self, register_map: RegisterMap) -> None:
        """HC2's mode register is not one of the basic seven."""
        rooms = ["heating_circuit_1", "heating_circuit_2"]
        _, basic = _controls(register_map, WriteLevel.BASIC, room_sensors=rooms)
        _, advanced = _controls(register_map, WriteLevel.ADVANCED, room_sensors=rooms)
        hc2 = register_map.at(200).key
        assert basic[hc2] == {Control.SENSOR}
        assert advanced[hc2] == {Control.CLIMATE}


class TestRoomSensor:
    def test_without_a_room_sensor_there_is_no_climate(
        self, register_map: RegisterMap
    ) -> None:
        """DESIGN.md 9.2: a thermostat with no current temperature is misleading."""
        selected, controls = _controls(
            register_map, WriteLevel.BASIC, room_sensors=[]
        )
        by_address = {r.address: controls[r.key] for r in selected}
        assert Control.CLIMATE not in by_address[100]
        assert by_address[100] == {Control.SELECT}
        assert by_address[101] == {Control.NUMBER}

    def test_the_select_and_number_pair_covers_the_same_ground(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(register_map, WriteLevel.BASIC, room_sensors=[])
        writable = {
            r.address
            for r in selected
            if controls[r.key] & {Control.NUMBER, Control.SELECT, Control.WATER_HEATER}
        }
        assert writable == set(BASIC_SEVEN)

    def test_only_the_circuit_with_a_sensor_gets_a_climate(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(
            register_map, WriteLevel.ADVANCED, room_sensors=["heating_circuit_2"]
        )
        by_address = {r.address: controls[r.key] for r in selected}
        assert Control.CLIMATE not in by_address[100]
        assert by_address[200] == {Control.CLIMATE}


class TestControlAssignment:
    def test_every_register_gets_at_least_one_entity(
        self, register_map: RegisterMap
    ) -> None:
        for level in WriteLevel:
            selected, controls = _controls(register_map, level)
            assert set(controls) == {r.key for r in selected}
            assert all(kinds for kinds in controls.values()), level

    def test_two_state_registers_go_to_binary_sensor(
        self, register_map: RegisterMap
    ) -> None:
        selected, controls = _controls(register_map, WriteLevel.BASIC)
        for register in selected:
            if is_two_state(register):
                assert Control.BINARY_SENSOR in controls[register.key], register.key
            else:
                assert Control.BINARY_SENSOR not in controls[register.key], register.key

    def test_a_disabled_block_contributes_nothing(
        self, register_map: RegisterMap
    ) -> None:
        selected = select_registers(register_map, blocks=["heat_pump"])
        controls = assign_controls(selected, write_level=WriteLevel.ADVANCED)
        assert not any(
            Control.WATER_HEATER in kinds for kinds in controls.values()
        )
