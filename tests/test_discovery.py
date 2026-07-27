"""Tier assignment, register selection and the block presence heuristic.

All of this is Home Assistant free, so it is tested directly against the real
register map rather than through a running integration.
"""

from __future__ import annotations

import pytest
from fujitsu_waterstage.codec import DecodedValue
from fujitsu_waterstage.const import (
    ALWAYS_ON_BLOCKS,
    DISCOVERABLE_BLOCKS,
    Tier,
    WriteLevel,
)
from fujitsu_waterstage.discovery import (
    analyse_blocks,
    async_run_discovery,
    find_room_sensors,
    is_board_local,
    select_registers,
    tier_for_register,
    tier_registers,
)
from fujitsu_waterstage.hub import MbioClient, ModbusGateway, build_read_groups
from fujitsu_waterstage.registers import RegisterMap

from .fake_board import FakeClient, make_board


class TestTiers:
    @pytest.mark.parametrize(
        ("address", "tier"),
        [
            (0, Tier.FAST),  # link status, no refresh_s of its own
            (1, Tier.FAST),  # heat pump status, 15 s
            (13, Tier.FAST),  # heat exchanger, 5 s
            (10, Tier.FAST),  # modulation, 30 s
            (400, Tier.FAST),  # active error count, 30 s
            (2, Tier.NORMAL),  # compressor status, 60 s
            (63, Tier.NORMAL),  # DHW temperature, 60 s
            (15, Tier.SLOW),  # flow setpoint, 120 s
            (41, Tier.SLOW),  # DHW setpoint, 255 s
            (440, Tier.STATIC),  # RVS software version, 0
            (9900, Tier.STATIC),  # product code, no refresh_s
            (9906, Tier.STATIC),  # serial format, no refresh_s
            (9908, Tier.NORMAL),  # interface uptime -- changes, but not via BSB
            (9921, Tier.NORMAL),  # BSB bus peak utilisation
        ],
    )
    def test_known_registers(
        self, register_map: RegisterMap, address: int, tier: Tier
    ) -> None:
        assert tier_for_register(register_map.at(address)) is tier

    def test_never_polls_faster_than_the_board_refreshes(
        self, register_map: RegisterMap
    ) -> None:
        """The one rule that must hold for every register in the map."""
        for register in register_map:
            tier = tier_for_register(register)
            assert tier.default_interval >= (register.refresh_s or 0), register.key

    def test_every_register_lands_in_exactly_one_tier(
        self, register_map: RegisterMap
    ) -> None:
        buckets = tier_registers(register_map.registers)
        keys = [r.key for regs in buckets.values() for r in regs]
        assert sorted(keys) == sorted(register_map.keys)

    def test_empty_tiers_are_dropped(self, register_map: RegisterMap) -> None:
        buckets = tier_registers(register_map.in_blocks(["swimming_pool"]))
        assert set(buckets) == {Tier.NORMAL, Tier.SLOW}


class TestOrigin:
    @pytest.mark.parametrize("address", [0, 9900, 9908, 9921, 13])
    def test_board_local(self, register_map: RegisterMap, address: int) -> None:
        assert is_board_local(register_map.at(address))

    @pytest.mark.parametrize("address", [1, 7, 41, 100, 400, 440, 450])
    def test_from_the_controller(self, register_map: RegisterMap, address: int) -> None:
        assert not is_board_local(register_map.at(address))


class TestSelectRegisters:
    def test_default_hides_the_expert_registers(self, register_map: RegisterMap) -> None:
        """38, 39, 460, 461 and 9907 switch hardware -- no entity by default."""
        selected = select_registers(register_map)
        assert not any(register.expert_only for register in selected)
        assert len(selected) == len(register_map) - 5

    def test_expert_level_includes_them(self, register_map: RegisterMap) -> None:
        selected = select_registers(register_map, write_level=WriteLevel.EXPERT)
        assert len(selected) == len(register_map)
        assert {r.address for r in selected if r.expert_only} == {38, 39, 460, 461, 9907}

    def test_advanced_still_hides_them(self, register_map: RegisterMap) -> None:
        selected = select_registers(register_map, write_level=WriteLevel.ADVANCED)
        assert not any(register.expert_only for register in selected)

    def test_block_filter(self, register_map: RegisterMap) -> None:
        selected = select_registers(register_map, blocks=["swimming_pool", "solar"])
        assert {r.address for r in selected} == {80, 90, 91, 92}

    def test_unknown_block_selects_nothing(self, register_map: RegisterMap) -> None:
        assert select_registers(register_map, blocks=["nope"]) == ()


def _all_zero(register_map: RegisterMap) -> dict[str, DecodedValue]:
    return {register.key: DecodedValue(0, False) for register in register_map}


class TestBlockHeuristic:
    def test_always_on_blocks_survive_an_empty_read(
        self, register_map: RegisterMap
    ) -> None:
        result = analyse_blocks(register_map, [_all_zero(register_map)])
        for block in ALWAYS_ON_BLOCKS:
            assert result.blocks[block] is True, block

    def test_silent_blocks_are_excluded(self, register_map: RegisterMap) -> None:
        result = analyse_blocks(register_map, [_all_zero(register_map)])
        assert set(result.excluded) == DISCOVERABLE_BLOCKS

    def test_a_live_status_code_enables_the_block(
        self, register_map: RegisterMap
    ) -> None:
        values = _all_zero(register_map)
        values["solar_solar_status"] = DecodedValue(137, False)
        result = analyse_blocks(register_map, [values])
        assert result.blocks["solar"] is True
        assert result.reasons["solar"] == "register 80 reported 137"

    def test_a_temperature_alone_does_not_enable_a_block_with_a_status(
        self, register_map: RegisterMap
    ) -> None:
        """Hardware, 2026-07-27: HC2 and CC2 are not fitted, yet register 224
        reported 50.0 °C and register 262 reported 140.0 °C.  Both are fixed
        placeholders, and taking them as evidence invented two circuits."""
        values = _all_zero(register_map)
        values["heating_circuit_2_room_temperature_2_actual_value"] = DecodedValue(
            50.0, False
        )
        values["cooling_circuit_2_flow_temperature_resulting_setpoint_cc2"] = (
            DecodedValue(140.0, False)
        )
        result = analyse_blocks(register_map, [values])
        assert result.blocks["heating_circuit_2"] is False
        assert result.blocks["cooling_circuit_2"] is False
        assert "status register" in result.reasons["heating_circuit_2"]

    def test_an_installation_with_no_hot_water_tank(
        self, register_map: RegisterMap
    ) -> None:
        """Confirmed by the owner: no DHW is plumbed in.

        The setpoints still hold configured values -- mode "On", 55.0 °C -- so
        only the status register tells the truth.
        """
        values = _all_zero(register_map)
        values["dhw_dhw_operating_mode"] = DecodedValue(1, False)
        values["dhw_dhw_nominal_temperature_setpoint"] = DecodedValue(55.0, False)

        result = analyse_blocks(register_map, [values])
        assert result.blocks["dhw"] is False

    def test_the_real_installation_is_classified_correctly(
        self, register_map: RegisterMap
    ) -> None:
        """Every value here came off the board on 2026-07-27."""
        values = _all_zero(register_map)
        for address, value in (
            (160, 119),  # cooling circuit 1: "24-hour Eco active"
            (162, 26.9),
            (224, 50.0),  # HC2 room temperature placeholder
            (225, 4.0),
            (262, 140.0),  # CC2 flow setpoint placeholder
            (40, 1),  # DHW configured, but nothing is connected to it
            (41, 55.0),
        ):
            values[register_map.at(address).key] = DecodedValue(value, False)

        result = analyse_blocks(register_map, [values])
        assert set(result.enabled) == ALWAYS_ON_BLOCKS | {"cooling_circuit_1"}
        assert "dhw" in result.excluded

    def test_a_disabled_value_does_not_count(self, register_map: RegisterMap) -> None:
        """A /O disabled data point is not evidence of anything."""
        values = _all_zero(register_map)
        values["buffer_buffer_tank_status"] = DecodedValue(137, True)
        result = analyse_blocks(register_map, [values])
        assert result.blocks["buffer"] is False

    def test_a_setpoint_alone_does_not_count(self, register_map: RegisterMap) -> None:
        """HC2 setpoints hold values whether or not the circuit is plumbed in."""
        values = _all_zero(register_map)
        values["heating_circuit_2_room_comfort_temperature_setpoint_hc2"] = DecodedValue(
            21.0, False
        )
        result = analyse_blocks(register_map, [values])
        assert result.blocks["heating_circuit_2"] is False

    def test_either_round_is_enough(self, register_map: RegisterMap) -> None:
        """The first read may answer 0 while it is still fetching the value."""
        first = _all_zero(register_map)
        second = _all_zero(register_map)
        second["cooling_circuit_1_cooling_circuit_1_status"] = DecodedValue(119, False)
        result = analyse_blocks(register_map, [first, second])
        assert result.blocks["cooling_circuit_1"] is True

    def test_reasons_name_the_deciding_register(
        self, register_map: RegisterMap
    ) -> None:
        result = analyse_blocks(register_map, [_all_zero(register_map)])
        assert result.reasons["heat_pump"] == "always enabled"
        assert "0 or disabled" in result.reasons["solar"]


class TestRunDiscovery:
    def _client(self, client: FakeClient) -> MbioClient:
        gateway = ModbusGateway(
            "discovery.test",
            502,
            inter_request_delay=0.0,
            backoff=0.0,
            client_factory=lambda: client,
        )
        return MbioClient(gateway, 3)

    async def test_reads_the_board_twice(self, register_map: RegisterMap) -> None:
        fake = make_board()
        result = await async_run_discovery(
            self._client(fake), register_map, delay=0, rounds=2
        )
        groups = build_read_groups(
            register_map.registers, readable=register_map.addresses
        )
        reads = [call for call in fake.calls if call[0] == "read"]
        assert len(reads) == 2 * len(groups)
        assert result.blocks["heat_pump"] is True
        # The default board has a hot water tank, but no pool, no solar and no
        # second circuit.
        assert result.blocks["dhw"] is True
        assert set(result.excluded) == DISCOVERABLE_BLOCKS - {"dhw"}

    async def test_a_placeholder_room_temperature_is_not_a_sensor(
        self, register_map: RegisterMap
    ) -> None:
        """Hardware, 2026-07-27: no room unit reads 50.0 °C, not 0."""
        values = _all_zero(register_map)
        values["heating_circuit_1_room_temperature_1_actual_value"] = DecodedValue(
            50.0, False
        )
        assert find_room_sensors(register_map, [values]) == ()

    async def test_a_real_room_temperature_is_a_sensor(
        self, register_map: RegisterMap
    ) -> None:
        values = _all_zero(register_map)
        values["heating_circuit_1_room_temperature_1_actual_value"] = DecodedValue(
            21.5, False
        )
        assert find_room_sensors(register_map, [values]) == ("heating_circuit_1",)

    async def test_finds_a_swimming_pool(self, register_map: RegisterMap) -> None:
        fake = make_board()
        fake.words[92] = 137  # the pool's status register reports a real code
        result = await async_run_discovery(
            self._client(fake), register_map, delay=0, rounds=1
        )
        assert result.blocks["swimming_pool"] is True

    async def test_a_rejected_group_does_not_stop_discovery(
        self, register_map: RegisterMap
    ) -> None:
        fake = make_board()
        fake.rejected = {9900}  # the board refuses the interface block
        result = await async_run_discovery(
            self._client(fake), register_map, delay=0, rounds=1
        )
        assert result.blocks["heat_pump"] is True

    async def test_total_failure_enables_everything(
        self, register_map: RegisterMap
    ) -> None:
        """A wrong guess is fixable in the options; a failed setup is not."""
        fake = FakeClient(fail_reads=10_000)
        result = await async_run_discovery(
            self._client(fake), register_map, delay=0, rounds=1
        )
        assert set(result.enabled) == set(register_map.blocks)
