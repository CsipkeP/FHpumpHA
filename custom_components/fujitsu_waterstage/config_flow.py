"""Config and options flow.

Setup is deliberately suspicious.  The RS-485 gateway usually carries other
devices, and writing to a neighbour's registers because a slave id was mistyped
would be a genuinely damaging bug.  So validation reads register 9900 and
insists on the MBIO product code ``0x0401``; anything else is reported as an
unknown device and the flow stops there rather than probing further
(DESIGN.md section 11).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ALWAYS_ON_BLOCKS,
    BLOCK_INTERFACE,
    CONF_BLOCKS,
    CONF_DISCOVERY_REASONS,
    CONF_FRAMING,
    CONF_FUNCTION_CODE,
    CONF_INTER_REQUEST_DELAY_MS,
    CONF_MAX_REGISTERS,
    CONF_RETRIES,
    CONF_ROOM_SENSORS,
    CONF_SCAN_INTERVAL_BY_TIER,
    CONF_SCAN_INTERVAL_FAST,
    CONF_SCAN_INTERVAL_NORMAL,
    CONF_SCAN_INTERVAL_SLOW,
    CONF_SLAVE_ID,
    CONF_TIMEOUT,
    CONF_WRITE_LEVEL,
    DEFAULT_INTER_REQUEST_DELAY_MS,
    DEFAULT_MAX_REGISTERS,
    DEFAULT_NAME,
    DEFAULT_RETRIES,
    DEFAULT_SLAVE_ID,
    DEFAULT_TIMEOUT,
    DEFAULT_WRITE_LEVEL,
    DOMAIN,
    HEATING_CIRCUITS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PRODUCT_CODE_ADDRESS,
    SETUP_SECOND_READ_DELAY,
    TIER_REFRESH_LIMIT,
    WriteLevel,
)
from .discovery import async_run_discovery
from .entity import format_board_serial
from .hub import (
    DEFAULT_PORT,
    FRAMINGS,
    FUNCTION_READ_HOLDING,
    FUNCTION_READ_INPUT,
    MBIO_PRODUCT_CODE,
    MbioClient,
    MbioError,
    async_get_gateway,
    async_release_gateway,
)
from .registers import load_register_map

_LOGGER = logging.getLogger(__name__)

#: Registers 9900..9903: product code, version, serial high, serial low.
_IDENTIFY_COUNT = 4

#: Discovery reads the whole map twice; if the bus is that slow, give up on it
#: and let the options flow sort the blocks out instead of failing the setup.
_DISCOVERY_TIMEOUT = 90

#: Gap between the two discovery rounds.  Module level so tests can shorten it.
_DISCOVERY_DELAY = SETUP_SECOND_READ_DELAY

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=65535, mode="box")
        ),
        vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=247, mode="box")
        ),
        vol.Required(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
    }
)


class CannotConnect(Exception):
    """The gateway or the slave did not answer."""


class UnknownDevice(Exception):
    """Something answered, but it is not an MBIO board."""


async def async_identify(host: str, port: int, slave_id: int) -> dict[str, Any]:
    """Prove that an MBIO board is listening, and read what identifies it.

    Both read function codes are tried: they map onto the same holding register
    space, but not every gateway and firmware answers both, and the one that
    works has to be remembered for the entry (DESIGN.md section 1).
    """
    gateway = await async_get_gateway(host, port)
    try:
        words: tuple[int, ...] | None = None
        function_code = FUNCTION_READ_HOLDING
        framing = gateway.framing
        errors: list[str] = []
        # Framing first: a raw RTU frame sent to a protocol converting gateway
        # gets no answer at all, from any slave and any function code, which
        # looks exactly like a dead bus.
        for candidate_framing in FRAMINGS:
            await gateway.async_set_framing(candidate_framing)
            for candidate in (FUNCTION_READ_HOLDING, FUNCTION_READ_INPUT):
                try:
                    words = await gateway.async_read_registers(
                        PRODUCT_CODE_ADDRESS,
                        _IDENTIFY_COUNT,
                        slave=slave_id,
                        function_code=candidate,
                    )
                except MbioError as err:
                    errors.append(f"{candidate_framing}/{candidate:#04x}: {err}")
                    continue
                function_code, framing = candidate, candidate_framing
                break
            if words is not None:
                break

        if words is None:
            raise CannotConnect("; ".join(errors))

        if words[0] != MBIO_PRODUCT_CODE:
            # Do not keep probing.  There is very likely another device on this
            # gateway, and its registers are none of our business.
            raise UnknownDevice(
                f"register {PRODUCT_CODE_ADDRESS} answered {words[0]:#06x}, "
                f"expected {MBIO_PRODUCT_CODE:#06x}"
            )

        return {
            CONF_FRAMING: framing,
            CONF_FUNCTION_CODE: function_code,
            "product_code": words[0],
            "version": words[1],
            "serial": format_board_serial(words[2], words[3]),
        }
    finally:
        await async_release_gateway(gateway)


class WaterstageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one MBIO board."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway address and verify what is behind it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            slave_id = int(user_input[CONF_SLAVE_ID])
            name = user_input[CONF_NAME]

            try:
                info = await async_identify(host, port, slave_id)
            except UnknownDevice as err:
                _LOGGER.warning("Not an MBIO board at %s:%s/%s: %s", host, port, slave_id, err)
                errors["base"] = "unknown_device"
            except CannotConnect as err:
                _LOGGER.debug("Cannot reach %s:%s/%s: %s", host, port, slave_id, err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - the flow must not crash on us
                _LOGGER.exception("Unexpected error while identifying the board")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    info["serial"] or f"{host}:{port}:{slave_id}"
                )
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port}
                )
                options = await self._async_initial_options(host, port, slave_id, info)
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SLAVE_ID: slave_id,
                        CONF_FRAMING: info[CONF_FRAMING],
                        CONF_FUNCTION_CODE: info[CONF_FUNCTION_CODE],
                    },
                    options=options,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def _async_initial_options(
        self, host: str, port: int, slave_id: int, info: dict[str, Any]
    ) -> dict[str, Any]:
        """Run block discovery once, here, so later setups stay fast.

        Discovery needs two full read rounds ten seconds apart, which is far too
        slow to repeat on every Home Assistant restart.  The result is stored in
        the entry options and the user can override it (DESIGN.md section 6).
        """
        register_map = await self.hass.async_add_executor_job(load_register_map)
        gateway = await async_get_gateway(host, port, framing=info[CONF_FRAMING])
        await gateway.async_set_framing(info[CONF_FRAMING])
        client = MbioClient(gateway, slave_id, function_code=info[CONF_FUNCTION_CODE])
        try:
            async with asyncio.timeout(_DISCOVERY_TIMEOUT):
                result = await async_run_discovery(
                    client, register_map, delay=_DISCOVERY_DELAY
                )
            blocks = list(result.enabled)
            room_sensors = list(result.room_sensors)
            reasons = dict(result.reasons)
        except (TimeoutError, MbioError) as err:
            _LOGGER.warning(
                "Block discovery did not finish (%s); enabling every block, "
                "adjust them in the integration options",
                err,
            )
            blocks = list(register_map.blocks)
            room_sensors = []
            reasons = dict.fromkeys(blocks, "discovery failed, enabled by default")
        finally:
            await async_release_gateway(gateway)

        return {
            CONF_BLOCKS: blocks,
            CONF_ROOM_SENSORS: room_sensors,
            CONF_DISCOVERY_REASONS: reasons,
            CONF_WRITE_LEVEL: DEFAULT_WRITE_LEVEL.value,
        }

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> WaterstageOptionsFlow:
        """Options are editable after setup."""
        return WaterstageOptionsFlow()


class WaterstageOptionsFlow(OptionsFlow):
    """Blocks, polling intervals, transport tuning and the write level."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(data=self._normalise(user_input))

        register_map = await self.hass.async_add_executor_job(load_register_map)
        options = self.config_entry.options
        enabled = set(options.get(CONF_BLOCKS, register_map.blocks))

        # The interface block carries the link status that every other entity's
        # availability depends on, so it is not offered as a choice.
        choices = [name for name in register_map.blocks if name != BLOCK_INTERFACE]

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BLOCKS,
                    default=sorted(enabled & set(choices)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices, multiple=True, mode="list", translation_key="blocks"
                    )
                ),
                vol.Optional(
                    CONF_ROOM_SENSORS,
                    default=sorted(options.get(CONF_ROOM_SENSORS, [])),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[circuit.block for circuit in HEATING_CIRCUITS],
                        multiple=True,
                        mode="list",
                        translation_key="blocks",
                    )
                ),
                vol.Required(
                    CONF_WRITE_LEVEL,
                    default=options.get(CONF_WRITE_LEVEL, DEFAULT_WRITE_LEVEL.value),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[level.value for level in WriteLevel],
                        translation_key="write_level",
                    )
                ),
                **self._interval_schema(options),
                vol.Required(
                    CONF_INTER_REQUEST_DELAY_MS,
                    default=options.get(
                        CONF_INTER_REQUEST_DELAY_MS, DEFAULT_INTER_REQUEST_DELAY_MS
                    ),
                ): _number(0, 1000),
                vol.Required(
                    CONF_TIMEOUT, default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): _number(1, 60),
                vol.Required(
                    CONF_RETRIES, default=options.get(CONF_RETRIES, DEFAULT_RETRIES)
                ): _number(1, 10),
                vol.Required(
                    CONF_MAX_REGISTERS,
                    default=options.get(CONF_MAX_REGISTERS, DEFAULT_MAX_REGISTERS),
                ): _number(1, 120),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    def _interval_schema(self, options: Mapping[str, Any]) -> dict[Any, Any]:
        """One interval per configurable tier, floored at its ``refresh_s``.

        Polling faster than the MBIO refreshes from the BSB bus only loads the
        bus, so the lower bound is the tier's own refresh rate rather than a
        flat number (DESIGN.md section 8.1).
        """
        schema: dict[Any, Any] = {}
        for tier, key in CONF_SCAN_INTERVAL_BY_TIER.items():
            floor = max(MIN_SCAN_INTERVAL, TIER_REFRESH_LIMIT[tier])
            schema[
                vol.Required(key, default=options.get(key, tier.default_interval))
            ] = _number(floor, MAX_SCAN_INTERVAL)
        return schema

    def _normalise(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Numbers arrive as floats from the selector; keep the entry tidy."""
        data = dict(user_input)
        data[CONF_BLOCKS] = sorted({*data.get(CONF_BLOCKS, []), *ALWAYS_ON_BLOCKS})
        stored = self.config_entry.options.get(CONF_DISCOVERY_REASONS)
        if stored:
            data[CONF_DISCOVERY_REASONS] = dict(stored)
        for key in (
            CONF_SCAN_INTERVAL_FAST,
            CONF_SCAN_INTERVAL_NORMAL,
            CONF_SCAN_INTERVAL_SLOW,
            CONF_INTER_REQUEST_DELAY_MS,
            CONF_TIMEOUT,
            CONF_RETRIES,
            CONF_MAX_REGISTERS,
        ):
            if key in data:
                data[key] = int(data[key])
        return data


def _number(minimum: float, maximum: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=minimum, max=maximum, mode="box", step=1)
    )
