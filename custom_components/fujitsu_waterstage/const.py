"""Constants for the Fujitsu Waterstage integration.

Deliberately free of Home Assistant imports so the polling model can be tested
without a Home Assistant installation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple

DOMAIN: Final = "fujitsu_waterstage"

# -- config entry data -------------------------------------------------------

CONF_SLAVE_ID: Final = "slave_id"
CONF_FUNCTION_CODE: Final = "function_code"

DEFAULT_NAME: Final = "Waterstage"
DEFAULT_SLAVE_ID: Final = 1

# -- config entry options ----------------------------------------------------

CONF_BLOCKS: Final = "blocks"
CONF_ROOM_SENSORS: Final = "room_sensors"
CONF_WRITE_LEVEL: Final = "write_level"
CONF_SCAN_INTERVAL_FAST: Final = "scan_interval_fast"
CONF_SCAN_INTERVAL_NORMAL: Final = "scan_interval_normal"
CONF_SCAN_INTERVAL_SLOW: Final = "scan_interval_slow"
CONF_INTER_REQUEST_DELAY_MS: Final = "inter_request_delay_ms"
CONF_TIMEOUT: Final = "timeout_s"
CONF_RETRIES: Final = "retries"
CONF_MAX_REGISTERS: Final = "max_registers_per_read"

DEFAULT_INTER_REQUEST_DELAY_MS: Final = 50
DEFAULT_TIMEOUT: Final = 5
DEFAULT_RETRIES: Final = 3
DEFAULT_MAX_REGISTERS: Final = 120

#: The options UI must not let the user poll faster than this (DESIGN.md 11).
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 3600


class WriteLevel(StrEnum):
    """How much of the parameter surface gets writable entities.

    ``basic`` is the default on purpose: the user wants to set a handful of
    values, not the whole controller (DESIGN.md 10.1).  Phase 3 creates no
    writable entities at all; the level already decides whether the ``expert``
    registers -- the ones that switch physical outputs -- exist as entities.
    """

    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"


DEFAULT_WRITE_LEVEL: Final = WriteLevel.BASIC


#: DESIGN.md 10.1 -- the only seven data points the ``basic`` level may write.
#: DHW mode and setpoint, HC1 mode, comfort, reduced, curve displacement and
#: the summer/winter changeover.  Everything else stays a read-only sensor, no
#: matter that the register map calls it ``R/W``.
BASIC_WRITE_ADDRESSES: frozenset[int] = frozenset({40, 41, 100, 101, 102, 106, 107})

#: ``R/W`` registers that are really counters: writing any value clears them.
#: DESIGN.md 10.1 lists 9912-9921 among the reset buttons even though the
#: register map marks them ``R/W``; a number box for "Modbus CRC error count"
#: would be nonsense.  9920 is read-only and stays out.
RESET_BY_WRITE_ADDRESSES: frozenset[int] = frozenset(
    {9912, 9913, 9914, 9915, 9916, 9917, 9918, 9919, 9921}
)

#: ``expert`` registers that perform an action rather than hold a value:
#: address -> (value to write, button label).
EXPERT_ACTIONS: Final[dict[int, tuple[int, str]]] = {
    38: (1, "Trigger defrost"),
    39: (1, "Reset heat pump"),
    9907: (0xAFAF, "Restart interface board"),
}

#: Any value clears an ``R/Reset`` counter; 0 is the obvious one.
RESET_VALUE: Final = 0

#: Seconds between a write and the targeted re-read of the affected group.
#: The controller needs a moment to take the value; until then the entity shows
#: the value that was written so the UI does not bounce (DESIGN.md section 10).
WRITE_REREAD_DELAY: Final = 2.0

#: How long an optimistic value survives if the re-read never lands.
WRITE_OPTIMISTIC_TTL: Final = 30.0


class Control(StrEnum):
    """Entity platforms this integration creates.

    The values match Home Assistant's ``Platform`` members, but this module
    stays import-free so the write-level rules can be tested on their own.
    """

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SELECT = "select"
    BUTTON = "button"
    CLIMATE = "climate"
    WATER_HEATER = "water_heater"


class Tier(StrEnum):
    """Polling tier.  DESIGN.md section 8.1."""

    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"
    STATIC = "static"

    @property
    def default_interval(self) -> int:
        """Default Home Assistant polling interval, in seconds."""
        return _TIER_DEFAULT_INTERVAL[self]

    @property
    def configurable(self) -> bool:
        """``STATIC`` values never change, so its interval is not an option."""
        return self is not Tier.STATIC


_TIER_DEFAULT_INTERVAL: Final[dict[Tier, int]] = {
    Tier.FAST: 30,
    Tier.NORMAL: 120,
    Tier.SLOW: 300,
    Tier.STATIC: 3600,
}

#: ``refresh_s`` upper bound for each tier -- how often the MBIO itself asks the
#: BSB bus.  Polling faster than this returns the same value and only loads the
#: bus, so it is the hard floor for the configured interval.
TIER_REFRESH_LIMIT: Final[dict[Tier, int]] = {
    Tier.FAST: 30,
    Tier.NORMAL: 60,
    Tier.SLOW: 255,
    Tier.STATIC: 0,
}

CONF_SCAN_INTERVAL_BY_TIER: Final[dict[Tier, str]] = {
    Tier.FAST: CONF_SCAN_INTERVAL_FAST,
    Tier.NORMAL: CONF_SCAN_INTERVAL_NORMAL,
    Tier.SLOW: CONF_SCAN_INTERVAL_SLOW,
}

# -- register roles ----------------------------------------------------------

#: Register 0 -- 0 means the MBIO cannot talk to the RVS21 over BSB.
LINK_STATUS_ADDRESS: Final = 0
LINK_STATUS_KEY: Final = "interface_communication_status"
LINK_STATUS_OK: Final = 1

#: Registers the MBIO measures itself, so they survive a BSB outage together
#: with the ``interface`` block (DESIGN.md section 4).
BOARD_LOCAL_ADDRESSES: Final = frozenset({13})

#: MBIO diagnostic counters.  They have no ``refresh_s`` because they do not
#: come from the BSB bus at all, but they do change, so they are not STATIC.
INTERFACE_DIAGNOSTIC_ADDRESSES: Final = frozenset(range(9908, 9922))

#: Board identification, read once (DESIGN.md 8.1, STATIC tier).
PRODUCT_CODE_ADDRESS: Final = 9900
VERSION_ADDRESS: Final = 9901
SERIAL_HIGH_ADDRESS: Final = 9902
SERIAL_LOW_ADDRESS: Final = 9903
RVS_VERSION_ADDRESS: Final = 440

# -- functional blocks -------------------------------------------------------

class HeatingCircuit(NamedTuple):
    """The registers one ``climate`` entity is built from (DESIGN.md 9.2)."""

    block: str
    #: Entity name.  Two circuits can share one device, so neither may take the
    #: device's own name.
    label: str
    #: Operating mode: protection / automatic / reduced / comfort.
    mode: int
    #: Room comfort setpoint -- the climate target temperature.
    comfort: int
    reduced: int
    #: Frost protection and maximum comfort, used as the UI temperature range
    #: so Home Assistant offers exactly what the controller accepts.
    frost_protection: int
    max_comfort: int
    #: Status code, translated into an HVAC action.
    status: int
    #: Room temperature.  Without a room sensor here there is no climate entity.
    room_temperature: int
    #: Whether the ``basic`` write level covers this circuit.
    basic: bool


HEATING_CIRCUITS: Final[tuple[HeatingCircuit, ...]] = (
    HeatingCircuit(
        "heating_circuit_1", "Heating circuit 1", 100, 101, 102, 103, 104, 120, 124, True
    ),
    HeatingCircuit(
        "heating_circuit_2", "Heating circuit 2", 200, 201, 202, 203, 204, 220, 224, False
    ),
)

#: Entity name of the water heater.  One per installation.
DHW_LABEL: Final = "Domestic hot water"

#: The ``water_heater`` entity: operating mode, nominal setpoint, B3 sensor.
DHW_MODE_ADDRESS: Final = 40
DHW_SETPOINT_ADDRESS: Final = 41
DHW_TEMPERATURE_ADDRESS: Final = 63

#: RVS status codes that mean the circuit is actively heating (DESIGN.md 9.2).
STATUS_CODES_HEATING: Final = frozenset({114, 116, 137})
#: ...and the ones that mean it is up but not calling for heat.
STATUS_CODES_IDLE: Final = frozenset({118, 162})

BLOCK_INTERFACE: Final = "interface"
BLOCK_HEAT_PUMP: Final = "heat_pump"
BLOCK_DHW: Final = "dhw"
BLOCK_FAULTS: Final = "faults"
BLOCK_RELAYS: Final = "relays"
BLOCK_HEATING_CIRCUIT_1: Final = "heating_circuit_1"

#: Always present: the heat pump itself, DHW, heating circuit 1, the fault log,
#: the relay feedback and the interface diagnostics (DESIGN.md section 6).
ALWAYS_ON_BLOCKS: Final = frozenset(
    {
        BLOCK_INTERFACE,
        BLOCK_HEAT_PUMP,
        BLOCK_DHW,
        BLOCK_HEATING_CIRCUIT_1,
        BLOCK_FAULTS,
        BLOCK_RELAYS,
    }
)

#: Present only in some hydraulic layouts; the discovery heuristic decides and
#: the user can override it in the options flow.
DISCOVERABLE_BLOCKS: Final = frozenset(
    {
        "heating_circuit_2",
        "cooling_circuit_1",
        "cooling_circuit_2",
        "solar",
        "buffer",
        "swimming_pool",
        "supplementary_source",
    }
)

# -- warm-up (DESIGN.md section 5) -------------------------------------------

#: The MBIO needs about four minutes after power-up to refresh every parameter
#: from the BSB bus.  Until then an exact 0 from a temperature register is more
#: likely "not read yet" than 0 °C, so it is published as unknown.
WARMUP_SECONDS: Final = 300

#: Setup reads everything twice; the first read triggers the BSB query, the
#: second one carries the answer.
SETUP_SECOND_READ_DELAY: Final = 10

# -- misc --------------------------------------------------------------------

MANUFACTURER_HEAT_PUMP: Final = "Fujitsu"
MANUFACTURER_BOARD: Final = "ACITECH Solutions"
MODEL_BOARD: Final = "FWS-MBIO-002"
DEFAULT_MODEL_HEAT_PUMP: Final = "Waterstage"

ATTR_CODE: Final = "code"
ATTR_RAW: Final = "raw"
ATTR_REGISTER: Final = "register"
