"""The write path, and the entities that use it.

Everything here goes through the real Home Assistant service calls, the real
validation, the real codec and the real gateway; only the socket is a fake, so
the raw words the tests assert on are the ones that would go out on the wire.
"""

from __future__ import annotations

import pytest
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_STEP,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.components.water_heater import (
    ATTR_AWAY_MODE,
    ATTR_OPERATION_LIST,
    ATTR_OPERATION_MODE,
    SERVICE_SET_AWAY_MODE,
    SERVICE_SET_OPERATION_MODE,
    STATE_ECO,
)
from homeassistant.components.water_heater import (
    DOMAIN as WATER_HEATER_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.fujitsu_waterstage.const import DOMAIN, WriteLevel

from .fake_board import make_board
from .helpers import entity_id_of, patch_board, poll, setup_integration, state_of

pytestmark = pytest.mark.usefixtures("auto_enable_custom_integrations")

HC1_MODE = "heating_circuit_1_operating_mode_heating_circuit_1"
HC1_COMFORT = "heating_circuit_1_room_comfort_temperature_setpoint_hc1"
HC1_REDUCED = "heating_circuit_1_room_reduced_temperature_setpoint_hc1"
HC1_DISPLACEMENT = "heating_circuit_1_heating_curve_1_parallel_displacement"
HC1_CHANGEOVER = "heating_circuit_1_summer_winter_changeover_temperature_hc1"
DHW_MODE = "dhw_dhw_operating_mode"
DHW_SETPOINT = "dhw_dhw_nominal_temperature_setpoint"


def writes(client) -> list[tuple[int, int]]:
    """Every single-register write the board saw, as (address, raw word)."""
    return [(call[1], call[2]) for call in client.calls if call[0] == "write"]


async def set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )


class TestNumber:
    async def test_bounds_come_from_the_register_map(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, HC1_REDUCED, "number")
        assert state.state == "18.0"
        assert state.attributes[ATTR_MIN] == 4.0
        assert state.attributes[ATTR_MAX] == 35.0
        assert state.attributes[ATTR_STEP] == 0.5

    async def test_writes_one_register_with_the_scaled_word(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await set_number(
                hass, entity_id_of(hass, entry, HC1_REDUCED, "number"), 19.5
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (102, 195) in writes(client)  # 19.5 °C in tenths

    async def test_negative_values_use_two_s_complement(
        self, hass: HomeAssistant
    ) -> None:
        """The heating curve displacement runs from -4.5 to 4.5 °C."""
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await set_number(
                hass, entity_id_of(hass, entry, HC1_DISPLACEMENT, "number"), -2.5
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (106, 0x10000 - 25) in writes(client)

    async def test_an_optional_register_is_never_written_as_disabled(
        self, hass: HomeAssistant
    ) -> None:
        """107 is R/W/O; a value that reads back disabled would be a bug."""
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await set_number(
                hass, entity_id_of(hass, entry, HC1_CHANGEOVER, "number"), 20.0
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (107, 200) in writes(client)
        assert not 200 & 0xC000  # neither the sign nor the disable bit
        assert state_of(hass, entry, HC1_CHANGEOVER, "number").state == "20.0"

    async def test_out_of_range_is_refused_before_the_bus(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_REDUCED, "number")
        with patch_board(client), pytest.raises(ServiceValidationError):
            await set_number(hass, entity_id, 40.0)  # the register stops at 35
        assert not writes(client)

    async def test_a_value_off_the_step_grid_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_REDUCED, "number")
        with patch_board(client), pytest.raises(ServiceValidationError):
            await set_number(hass, entity_id, 19.3)  # the step is 0.5
        assert not writes(client)

    async def test_a_failed_write_raises_and_changes_nothing(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_REDUCED, "number")
        client.write_error_response = True  # the board rejects the write

        with patch_board(client), pytest.raises(HomeAssistantError):
            await set_number(hass, entity_id, 19.5)
        assert state_of(hass, entry, HC1_REDUCED, "number").state == "18.0"

    async def test_the_everyday_seven_are_not_hidden_in_the_config_category(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass)
        registry = er.async_get(hass)
        entity = registry.async_get(entity_id_of(hass, entry, HC1_REDUCED, "number"))
        assert entity.entity_category is None

    async def test_advanced_numbers_are_configuration(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        registry = er.async_get(hass)
        entity = registry.async_get(
            entity_id_of(
                hass, entry, "heating_circuit_1_heating_curve_1_slope", "number"
            )
        )
        assert entity.entity_category is EntityCategory.CONFIG

    async def test_a_scaled_register_round_trips(self, hass: HomeAssistant) -> None:
        """The heating curve slope is stored in hundredths."""
        entry, client = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        entity_id = entity_id_of(
            hass, entry, "heating_circuit_1_heating_curve_1_slope", "number"
        )
        with patch_board(client):
            await set_number(hass, entity_id, 1.4)
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (105, 140) in writes(client)
        assert hass.states.get(entity_id).state == "1.4"


class TestSelect:
    async def _entry(self, hass: HomeAssistant, **kwargs):
        return await setup_integration(hass, room_sensors=[], **kwargs)

    async def test_options_come_from_the_register_map(
        self, hass: HomeAssistant
    ) -> None:
        """Without a room sensor the HC1 mode is a select, not a climate."""
        entry, _ = await self._entry(hass)
        state = state_of(hass, entry, HC1_MODE, "select")
        assert state.attributes[ATTR_OPTIONS] == [
            "Protection",
            "Automatic",
            "Reduced",
            "Comfort",
        ]
        assert state.state == "Automatic"

    async def test_writes_the_code_behind_the_text(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await self._entry(hass)
        with patch_board(client):
            await hass.services.async_call(
                SELECT_DOMAIN,
                SERVICE_SELECT_OPTION,
                {
                    ATTR_ENTITY_ID: entity_id_of(hass, entry, HC1_MODE, "select"),
                    ATTR_OPTION: "Comfort",
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (100, 3) in writes(client)

    async def test_a_non_zero_based_code_list(self, hass: HomeAssistant) -> None:
        """The cooling release uses 1 and 2, with no zero at all."""
        entry, client = await self._entry(hass, write_level=WriteLevel.ADVANCED)
        entity_id = entity_id_of(
            hass, entry, "cooling_circuit_1_release_cooling_circuit_1", "select"
        )
        with patch_board(client):
            await hass.services.async_call(
                SELECT_DOMAIN,
                SERVICE_SELECT_OPTION,
                {
                    ATTR_ENTITY_ID: entity_id,
                    ATTR_OPTION: "Time program cooling circuit",
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (143, 2) in writes(client)

    async def test_an_unknown_option_is_refused(self, hass: HomeAssistant) -> None:
        entry, client = await self._entry(hass)
        entity_id = entity_id_of(hass, entry, HC1_MODE, "select")
        with patch_board(client), pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                SELECT_DOMAIN,
                SERVICE_SELECT_OPTION,
                {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "Turbo"},
                blocking=True,
            )
        assert not writes(client)


class TestButton:
    async def test_none_at_the_basic_level(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert not [e for e in entities if e.domain == "button"]

    async def test_a_reset_writes_zero(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        entity_id = entity_id_of(
            hass, entry, "heat_pump_compressor_1_runtime", "button"
        )
        with patch_board(client):
            await hass.services.async_call(
                "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (24, 0) in writes(client)

    async def test_the_counter_keeps_its_sensor(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        assert state_of(hass, entry, "heat_pump_compressor_1_runtime") is not None
        assert (
            state_of(hass, entry, "heat_pump_compressor_1_runtime", "button") is not None
        )

    async def test_resets_are_diagnostic(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        registry = er.async_get(hass)
        entity = registry.async_get(
            entity_id_of(hass, entry, "heat_pump_compressor_1_runtime", "button")
        )
        assert entity.entity_category is EntityCategory.DIAGNOSTIC

    async def test_expert_actions_write_their_documented_value(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass, write_level=WriteLevel.EXPERT)
        with patch_board(client):
            for key, expected in (
                ("heat_pump_defrost_trigger", (38, 1)),
                ("heat_pump_reset_heat_pump", (39, 1)),
                (
                    "interface_oscillator_calibration_soft_restart",
                    (9907, 0xAFAF),
                ),
            ):
                await hass.services.async_call(
                    "button",
                    "press",
                    {ATTR_ENTITY_ID: entity_id_of(hass, entry, key, "button")},
                    blocking=True,
                )
                await hass.async_block_till_done(wait_background_tasks=True)
                assert expected in writes(client), key

    async def test_expert_actions_do_not_exist_below_expert(
        self, hass: HomeAssistant
    ) -> None:
        entry, _ = await setup_integration(hass, write_level=WriteLevel.ADVANCED)
        registry = er.async_get(hass)
        assert (
            registry.async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}_heat_pump_defrost_trigger"
            )
            is None
        )


class TestClimate:
    async def test_state_comes_from_four_registers(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, HC1_MODE, "climate")

        assert state.state == HVACMode.AUTO  # register 100 = 1
        assert state.attributes[ATTR_TEMPERATURE] == 21.0  # 101
        assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21.5  # 124
        assert state.attributes[ATTR_PRESET_MODE] == "automatic"
        assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING  # 120 = 137

    async def test_the_range_follows_the_controller(
        self, hass: HomeAssistant
    ) -> None:
        """Frost protection and maximum comfort, not a hard-coded 4 to 35."""
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, HC1_MODE, "climate")
        assert state.attributes[ATTR_MIN_TEMP] == 6.0  # register 103
        assert state.attributes[ATTR_MAX_TEMP] == 30.0  # register 104

    async def test_setting_the_temperature_writes_the_comfort_setpoint(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                "set_temperature",
                {
                    ATTR_ENTITY_ID: entity_id_of(hass, entry, HC1_MODE, "climate"),
                    ATTR_TEMPERATURE: 22.5,
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (101, 225) in writes(client)

    async def test_turning_it_off_selects_frost_protection(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_MODE, "climate")
        with patch_board(client):
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (100, 0) in writes(client)
        assert hass.states.get(entity_id).state == HVACMode.OFF

    async def test_heat_selects_comfort(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: entity_id_of(hass, entry, HC1_MODE, "climate"),
                    "hvac_mode": HVACMode.HEAT,
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (100, 3) in writes(client)

    async def test_the_reduced_preset_is_only_reachable_as_a_preset(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_MODE, "climate")
        with patch_board(client):
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_PRESET_MODE,
                {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "reduced"},
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (100, 2) in writes(client)
        state = hass.states.get(entity_id)
        assert state.attributes[ATTR_PRESET_MODE] == "reduced"
        assert state.state == HVACMode.HEAT  # reduced is still heating

    async def test_idle_when_the_circuit_is_in_summer_operation(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        client.words[120] = 118  # Summer operation
        await poll(hass, entry)
        state = state_of(hass, entry, HC1_MODE, "climate")
        assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE

    async def test_no_climate_without_a_room_sensor(
        self, hass: HomeAssistant
    ) -> None:
        """DESIGN.md 9.2 -- the select and number pair takes over instead."""
        entry, _ = await setup_integration(hass, room_sensors=[])
        registry = er.async_get(hass)
        assert (
            registry.async_get_entity_id(
                "climate", DOMAIN, f"{entry.entry_id}_{HC1_MODE}"
            )
            is None
        )
        assert state_of(hass, entry, HC1_MODE, "select") is not None
        assert state_of(hass, entry, HC1_COMFORT, "number") is not None

    async def test_a_dead_room_sensor_is_detected_at_setup(
        self, hass: HomeAssistant
    ) -> None:
        """No stored answer, and register 124 reads 0: no climate entity."""
        board = make_board()
        board.words[124] = 0
        entry, _ = await setup_integration(hass, board)
        registry = er.async_get(hass)
        assert (
            registry.async_get_entity_id(
                "climate", DOMAIN, f"{entry.entry_id}_{HC1_MODE}"
            )
            is None
        )

    async def test_no_hc2_climate_at_the_basic_level(
        self, hass: HomeAssistant
    ) -> None:
        """HC2's registers are not among the basic seven, room sensor or not."""
        board = make_board()
        board.words[224] = 205  # HC2 room temperature 20.5 °C
        entry, _ = await setup_integration(
            hass, board, room_sensors=["heating_circuit_1", "heating_circuit_2"]
        )
        hc2 = "heating_circuit_2_operating_mode_heating_circuit_2"

        assert (
            er.async_get(hass).async_get_entity_id(
                "climate", DOMAIN, f"{entry.entry_id}_{hc2}"
            )
            is None
        )
        assert state_of(hass, entry, hc2) is not None  # a plain sensor instead

    async def test_hc2_climate_at_the_advanced_level(
        self, hass: HomeAssistant
    ) -> None:
        board = make_board()
        board.words[224] = 205
        entry, _ = await setup_integration(
            hass,
            board,
            write_level=WriteLevel.ADVANCED,
            room_sensors=["heating_circuit_1", "heating_circuit_2"],
        )
        hc2 = "heating_circuit_2_operating_mode_heating_circuit_2"
        state = state_of(hass, entry, hc2, "climate")
        assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 20.5


class TestWaterHeater:
    async def test_state(self, hass: HomeAssistant) -> None:
        entry, _ = await setup_integration(hass)
        state = state_of(hass, entry, DHW_MODE, "water_heater")

        assert state.state == STATE_ON  # register 40 = 1
        assert state.attributes[ATTR_TEMPERATURE] == 50.0  # 41
        assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 48.0  # 63
        assert state.attributes[ATTR_OPERATION_LIST] == [STATE_OFF, STATE_ON, STATE_ECO]
        assert state.attributes[ATTR_MIN_TEMP] == 40.0
        assert state.attributes[ATTR_MAX_TEMP] == 65.0

    async def test_setting_the_temperature(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await hass.services.async_call(
                WATER_HEATER_DOMAIN,
                "set_temperature",
                {
                    ATTR_ENTITY_ID: entity_id_of(hass, entry, DHW_MODE, "water_heater"),
                    ATTR_TEMPERATURE: 52.0,
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (41, 520) in writes(client)

    async def test_a_setpoint_below_the_range_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, DHW_MODE, "water_heater")
        with patch_board(client), pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                WATER_HEATER_DOMAIN,
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: 20.0},
                blocking=True,
            )
        assert not writes(client)

    async def test_operation_mode(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        with patch_board(client):
            await hass.services.async_call(
                WATER_HEATER_DOMAIN,
                SERVICE_SET_OPERATION_MODE,
                {
                    ATTR_ENTITY_ID: entity_id_of(hass, entry, DHW_MODE, "water_heater"),
                    ATTR_OPERATION_MODE: STATE_ECO,
                },
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (40, 2) in writes(client)

    async def test_away_mode_is_eco(self, hass: HomeAssistant) -> None:
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, DHW_MODE, "water_heater")
        assert hass.states.get(entity_id).attributes[ATTR_AWAY_MODE] == STATE_OFF

        with patch_board(client):
            await hass.services.async_call(
                WATER_HEATER_DOMAIN,
                SERVICE_SET_AWAY_MODE,
                {ATTR_ENTITY_ID: entity_id, ATTR_AWAY_MODE: True},
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        assert (40, 2) in writes(client)
        assert hass.states.get(entity_id).attributes[ATTR_AWAY_MODE] == STATE_ON

    async def test_unavailable_when_the_bsb_link_drops(
        self, hass: HomeAssistant
    ) -> None:
        entry, client = await setup_integration(hass)
        client.words[0] = 0
        await poll(hass, entry)
        assert state_of(hass, entry, DHW_MODE, "water_heater").state == STATE_UNAVAILABLE


class TestWriteFollowUp:
    async def test_only_the_affected_group_is_re_read(
        self, hass: HomeAssistant
    ) -> None:
        """DESIGN.md section 10 -- not the other hundred registers of the tier."""
        entry, client = await setup_integration(hass)
        group = entry.runtime_data.coordinators[
            entry.runtime_data.coordinator_for(
                entry.runtime_data.register_map.at(102)
            ).tier
        ].group_for(entry.runtime_data.register_map.at(102))
        before = len(client.calls)

        with patch_board(client):
            await set_number(
                hass, entity_id_of(hass, entry, HC1_REDUCED, "number"), 19.5
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        reads = [call for call in client.calls[before:] if call[0] == "read"]
        assert len(reads) == 1
        assert reads[0][1] == group.start

    async def test_the_written_value_shows_before_the_re_read(
        self, hass: HomeAssistant
    ) -> None:
        """Optimistic state, so a slider does not snap back for two seconds."""
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_REDUCED, "number")
        register = entry.runtime_data.register_map.at(102)
        coordinator = entry.runtime_data.coordinator_for(register)
        original = client.words[102]

        # A board that keeps answering the old value, and a confirming read far
        # enough away that it has not happened yet.  The waits here are on
        # purpose not waiting for background tasks.
        with patch_board(client, reread_delay=60):
            await set_number(hass, entity_id, 19.5)
            client.words[102] = original
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "19.5"

            # An ordinary poll landing in between must not undo it either.
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "19.5"

    async def test_the_re_read_wins_in_the_end(self, hass: HomeAssistant) -> None:
        """If the controller clamped the value, the read is what survives."""
        entry, client = await setup_integration(hass)
        entity_id = entity_id_of(hass, entry, HC1_REDUCED, "number")

        with patch_board(client):
            await set_number(hass, entity_id, 19.5)
            client.words[102] = 190  # the controller settled on 19.0
            await hass.async_block_till_done(wait_background_tasks=True)

        assert hass.states.get(entity_id).state == "19.0"
