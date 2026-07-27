"""Register map tests.

Beyond the loader itself, these pin down the facts CLAUDE.md calls out: the
register keys must never change, register 2 is the compressor and not the HC1
pump, and no address may be invented that is not in the JSON.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fujitsu_waterstage.codec import RegisterType
from fujitsu_waterstage.registers import (
    REGISTER_MAP_FILE,
    RegisterMap,
    load_register_map,
    register_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_KEYS = Path(__file__).with_name("register_keys.json")


class TestLoading:
    def test_register_count(self, register_map: RegisterMap) -> None:
        assert len(register_map) == 204

    def test_packaged_copy_matches_docs(self) -> None:
        """A HACS install ships the package copy, so it must not drift."""
        docs = (REPO_ROOT / "docs" / "mbio_registers.json").read_bytes()
        assert REGISTER_MAP_FILE.read_bytes() == docs

    def test_cached(self) -> None:
        assert load_register_map() is load_register_map()

    def test_code_tables(self, register_map: RegisterMap) -> None:
        assert register_map.status_codes[137] == "Heating mode"
        assert register_map.error_codes[0] == "No Error"
        assert register_map.mbio_error_codes[0] == "No error"
        # JSON object keys are strings; register values are integers.
        assert all(isinstance(code, int) for code in register_map.status_codes)

    def test_unknown_code_table(self, register_map: RegisterMap) -> None:
        with pytest.raises(KeyError):
            register_map.code_table("nope")

    def test_blocks(self, register_map: RegisterMap) -> None:
        assert register_map.blocks[0] == "interface"
        assert set(register_map.blocks) == {
            "buffer",
            "cooling_circuit_1",
            "cooling_circuit_2",
            "dhw",
            "faults",
            "heat_pump",
            "heating_circuit_1",
            "heating_circuit_2",
            "interface",
            "relays",
            "solar",
            "supplementary_source",
            "swimming_pool",
        }

    def test_in_blocks(self, register_map: RegisterMap) -> None:
        selection = register_map.in_blocks(["swimming_pool"])
        assert [register.address for register in selection] == [90, 91, 92]

    def test_lookup(self, register_map: RegisterMap) -> None:
        assert register_map.at(7) is register_map["heat_pump_return_temperature"]
        assert register_map.at(3141) is None
        assert register_map.get("no_such_register") is None

    def test_length_matches_type(self, register_map: RegisterMap) -> None:
        for register in register_map:
            assert register.length == register.type.length

    def test_no_address_overlaps(self, register_map: RegisterMap) -> None:
        seen: dict[int, str] = {}
        for register in register_map:
            for address in register.addresses:
                assert address not in seen, f"{register.key} overlaps {seen[address]}"
                seen[address] = register.key


class TestKeys:
    def test_keys_are_unique(self, register_map: RegisterMap) -> None:
        keys = register_map.keys
        assert len(set(keys)) == len(keys)

    def test_keys_are_snake_case(self, register_map: RegisterMap) -> None:
        for key in register_map.keys:
            assert key
            assert key == key.lower()
            assert all(char.isalnum() or char == "_" for char in key)
            assert "__" not in key
            assert not key.startswith("_") and not key.endswith("_")

    def test_keys_never_change(self, register_map: RegisterMap) -> None:
        """``unique_id`` is built from these; a change orphans every entity."""
        golden = json.loads(GOLDEN_KEYS.read_text(encoding="utf-8"))
        current = {str(r.address): r.key for r in register_map}
        assert current == golden

    def test_degree_sign_and_accents_fold(self) -> None:
        assert (
            register_key("cooling_circuit_1", "Flow temperature at 25 °C outdoor temp CC1")
            == "cooling_circuit_1_flow_temperature_at_25_c_outdoor_temp_cc1"
        )
        assert register_key("x", "Külső érzékelő") == "x_kulso_erzekelo"

    def test_deterministic(self) -> None:
        assert register_key("dhw", "DHW status") == register_key("dhw", "DHW status")


class TestAccessFlags:
    def test_read_only(self, register_map: RegisterMap) -> None:
        register = register_map.at(7)
        assert not register.writable
        assert not register.optional
        assert not register.resettable

    def test_writable(self, register_map: RegisterMap) -> None:
        assert register_map.at(41).writable  # R/W
        assert register_map.at(22).writable  # R/W/O
        assert register_map.at(107).writable

    def test_optional(self, register_map: RegisterMap) -> None:
        assert register_map.at(13).optional  # R/O
        assert register_map.at(107).optional  # R/W/O
        assert not register_map.at(101).optional  # R/W

    def test_reset_is_not_optional(self, register_map: RegisterMap) -> None:
        """``R/Reset`` must not be mistaken for ``R/O``."""
        register = register_map.at(18)
        assert register.resettable
        assert not register.optional
        assert not register.writable

    def test_every_reset_register(self, register_map: RegisterMap) -> None:
        addresses = [r.address for r in register_map if r.resettable]
        assert addresses == [18, 19, 20, 24, 26, 28, 30, 32, 34, 37, 68, 70, 72, 74]

    def test_expert_registers(self, register_map: RegisterMap) -> None:
        """DESIGN.md section 10.1 -- these switch hardware."""
        assert [r.address for r in register_map if r.expert_only] == [38, 39, 460, 461, 9907]

    def test_link_status_register(self, register_map: RegisterMap) -> None:
        link = register_map.at(0)
        assert link.is_link_status
        assert link.options == {0: "BSB communication error", 1: "OK"}
        assert sum(r.is_link_status for r in register_map) == 1


class TestKnownFacts:
    """Facts from CLAUDE.md that the map must keep agreeing with."""

    def test_register_2_is_the_compressor_not_the_hc1_pump(
        self, register_map: RegisterMap
    ) -> None:
        assert register_map.at(2).name == "Compressor 1 status"
        assert register_map.at(2).options == {0: "Off", 255: "On"}
        assert register_map.at(121).name == "Pump status HC1"

    def test_on_off_is_zero_and_255(self, register_map: RegisterMap) -> None:
        assert register_map.at(121).options == {0: "Off", 255: "On"}

    def test_room_thermostat_is_zero_and_one(self, register_map: RegisterMap) -> None:
        """The documented exception to the 0/255 rule."""
        assert register_map.at(128).options == {0: "No demand", 1: "Demand"}
        assert register_map.at(228).options == {0: "No demand", 1: "Demand"}

    def test_basic_write_level_registers_exist_and_are_writable(
        self, register_map: RegisterMap
    ) -> None:
        for address in (40, 41, 100, 101, 102, 106, 107):
            register = register_map.at(address)
            assert register is not None, address
            assert register.writable, address

    def test_temperatures_are_the_temp_type(self, register_map: RegisterMap) -> None:
        for address in (7, 8, 9, 17, 63, 101, 124):
            assert register_map.at(address).type is RegisterType.TEMP

    def test_multi_register_pairs(self, register_map: RegisterMap) -> None:
        for register in register_map:
            if register.type in (RegisterType.UINT32, RegisterType.DTIME):
                assert register.length == 2
                assert register.end_address == register.address + 1

    def test_fault_history_timestamps(self, register_map: RegisterMap) -> None:
        addresses = [r.address for r in register_map if r.type is RegisterType.DTIME]
        assert addresses == list(range(412, 431, 2))


class TestDecodeThroughRegister:
    def test_temp_optional(self, register_map: RegisterMap) -> None:
        """Register 13 is R/O -- the only plain R/O temp in the map."""
        register = register_map.at(13)
        assert register.decode([0x0065]) == (10.1, False)
        assert register.decode([0x4065]) == (10.1, True)

    def test_temp_plain(self, register_map: RegisterMap) -> None:
        assert register_map.at(7).decode([0xFF9B]) == (-10.1, False)

    def test_uint16_scaled(self, register_map: RegisterMap) -> None:
        assert register_map.at(105).decode([250]) == (2.5, False)
        assert register_map.at(23).decode([35]) == (3.5, False)
        assert register_map.at(9905).decode([960]) == (9600, False)

    def test_uint32(self, register_map: RegisterMap) -> None:
        assert register_map.at(24).decode([0x0001, 0x0000]) == (65536, False)

    def test_dtime(self, register_map: RegisterMap) -> None:
        assert register_map.at(412).decode([0x07B4, 0x5AD4]) == (
            datetime(2023, 4, 11, 11, 20),
            False,
        )

    def test_describe_inline_options(self, register_map: RegisterMap) -> None:
        assert register_map.describe(register_map.at(2), 255) == "On"
        assert register_map.describe(register_map.at(2), 7) is None

    def test_describe_referenced_table(self, register_map: RegisterMap) -> None:
        assert register_map.describe(register_map.at(1), 137) == "Heating mode"
        assert register_map.describe(register_map.at(401), 0) == "No Error"
        assert register_map.describe(register_map.at(9912), 0) == "No error"

    def test_describe_plain_register(self, register_map: RegisterMap) -> None:
        assert register_map.describe(register_map.at(7), 100) is None


class TestValidation:
    def test_range(self, register_map: RegisterMap) -> None:
        register = register_map.at(41)  # DHW setpoint, 40..65 °C, step 1
        register.validate(50.0)
        with pytest.raises(ValueError):
            register.validate(39.0)
        with pytest.raises(ValueError):
            register.validate(66.0)

    def test_step(self, register_map: RegisterMap) -> None:
        register = register_map.at(101)  # 4..35 °C, step 0.5
        register.validate(21.5)
        with pytest.raises(ValueError):
            register.validate(21.3)
        register.validate(21.3, check_step=False)

    def test_fractional_step(self, register_map: RegisterMap) -> None:
        register = register_map.at(105)  # slope 0.1..4.0, step 0.02
        register.validate(2.5)
        register.validate(0.1)
        with pytest.raises(ValueError):
            register.validate(0.11)

    def test_options(self, register_map: RegisterMap) -> None:
        register = register_map.at(40)  # DHW operating mode 0/1/2
        register.validate(2)
        with pytest.raises(ValueError):
            register.validate(3)


class TestRoundTrip:
    """``decode(encode(x)) == x`` for every writable register."""

    def _candidates(self, register) -> list[float]:
        if register.options is not None:
            return [float(code) for code in register.options]
        if register.minimum is None or register.maximum is None:
            # No documented range: exercise the raw span instead.
            scale = register.scale or 1
            return [0.0, 1.0 * scale, 100.0 * scale, 65535.0 * scale]
        step = register.step or (0.1 if register.type is RegisterType.TEMP else 1)
        values = [register.minimum, register.maximum]
        value = register.minimum
        while value < register.maximum:
            values.append(value)
            value = round(value + step, 6)
        return values

    def test_every_writable_register(self, register_map: RegisterMap) -> None:
        writable = [r for r in register_map if r.writable]
        assert len(writable) == 66 + 18  # R/W plus R/W/O
        for register in writable:
            for value in self._candidates(register):
                words = register.encode(value)
                assert len(words) == register.length, register.key
                assert all(0 <= word <= 0xFFFF for word in words), register.key
                decoded = register.decode(words)
                assert not decoded.disabled, f"{register.key} encoded as disabled"
                assert decoded.value == pytest.approx(value), (
                    f"{register.key} round trip failed for {value}"
                )

    def test_every_reset_register_accepts_zero(self, register_map: RegisterMap) -> None:
        for register in register_map:
            if register.resettable:
                assert register.encode(0) == (0,) * register.length
