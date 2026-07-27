"""Constants for the Fujitsu Waterstage integration.

Deliberately free of Home Assistant imports so the polling model can be tested
without a Home Assistant installation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "fujitsu_waterstage"

# -- config entry data -------------------------------------------------------

CONF_SLAVE_ID: Final = "slave_id"
CONF_FUNCTION_CODE: Final = "function_code"

DEFAULT_NAME: Final = "Waterstage"
DEFAULT_SLAVE_ID: Final = 1

# -- config entry options ----------------------------------------------------

CONF_BLOCKS: Final = "blocks"
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
