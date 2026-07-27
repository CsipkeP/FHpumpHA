"""Which registers exist, where they belong and how often they are read.

Three separate questions, all answerable without Home Assistant:

* **Tier** -- how often a register may be polled.  Driven by the ``refresh_s``
  field, which states how often the MBIO itself queries the BSB bus; polling
  faster returns the same value and only loads the bus (DESIGN.md section 8.1).
* **Origin** -- whether a value comes from the RVS21 over BSB, or from the MBIO
  board itself.  This decides both which Home Assistant device owns the entity
  and whether a BSB outage makes it unavailable (DESIGN.md sections 4 and 9.3).
* **Block presence** -- whether a hydraulic block (HC2, cooling, solar, buffer,
  pool, supplementary source) exists in this installation at all.  There is no
  hydraulic-scheme register, so this is a heuristic over two full read rounds
  and the user can override it (DESIGN.md section 6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .codec import DecodedValue, RegisterType
from .const import (
    ALWAYS_ON_BLOCKS,
    BASIC_WRITE_ADDRESSES,
    BOARD_LOCAL_ADDRESSES,
    DHW_MODE_ADDRESS,
    DHW_SETPOINT_ADDRESS,
    DISCOVERABLE_BLOCKS,
    EXPERT_ACTIONS,
    HEATING_CIRCUITS,
    INTERFACE_DIAGNOSTIC_ADDRESSES,
    RESET_BY_WRITE_ADDRESSES,
    ROOM_TEMPERATURE_RANGE,
    SETUP_SECOND_READ_DELAY,
    Control,
    Tier,
    WriteLevel,
)
from .hub import MbioClient, ReadGroup, build_read_groups
from .registers import Register, RegisterMap

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DiscoveryResult",
    "analyse_blocks",
    "assign_controls",
    "async_run_discovery",
    "is_board_local",
    "is_two_state",
    "select_registers",
    "tier_for_register",
    "tier_registers",
    "write_allowed",
]


def tier_for_register(register: Register) -> Tier:
    """Assign a polling tier, DESIGN.md section 8.1.

    ``refresh_s`` decides, with two documented exceptions: the link status has
    no ``refresh_s`` but is the availability switch and must be read often, and
    the MBIO's own diagnostic counters have none either because they never touch
    the BSB bus -- they still change, so they are not static.
    """
    if register.is_link_status:
        return Tier.FAST
    if register.address in INTERFACE_DIAGNOSTIC_ADDRESSES:
        return Tier.NORMAL

    refresh_s = register.refresh_s
    if not refresh_s:  # absent or 0 -- identification and trigger registers
        return Tier.STATIC
    if refresh_s <= Tier.FAST.default_interval:
        return Tier.FAST
    if refresh_s <= 60:
        return Tier.NORMAL
    return Tier.SLOW


def is_board_local(register: Register) -> bool:
    """Whether the MBIO produces this value itself, without the BSB bus.

    Board-local values stay valid when register 0 reports a BSB failure, and
    they belong to the interface device in the device registry.
    """
    return (
        register.block == "interface" or register.address in BOARD_LOCAL_ADDRESSES
    )


def select_registers(
    register_map: RegisterMap,
    *,
    blocks: Iterable[str] | None = None,
    write_level: WriteLevel | str = WriteLevel.BASIC,
) -> tuple[Register, ...]:
    """The registers this configuration should read.

    Blocks the user (or discovery) turned off are dropped, and so are the
    ``safety: expert`` registers unless the expert write level is active --
    those trigger defrosts, restart the heat pump or switch physical outputs,
    so they should not even exist as entities by default (DESIGN.md 10.1).
    """
    enabled = set(register_map.blocks) if blocks is None else set(blocks)
    expert = WriteLevel(write_level) is WriteLevel.EXPERT
    return tuple(
        register
        for register in register_map
        if register.block in enabled and (expert or not register.expert_only)
    )


def tier_registers(registers: Iterable[Register]) -> dict[Tier, tuple[Register, ...]]:
    """Split registers into the four polling tiers, dropping empty ones."""
    buckets: dict[Tier, list[Register]] = {tier: [] for tier in Tier}
    for register in registers:
        buckets[tier_for_register(register)].append(register)
    return {tier: tuple(items) for tier, items in buckets.items() if items}


# ---------------------------------------------------------------------------
# Which entity a register turns into
# ---------------------------------------------------------------------------


def is_two_state(register: Register) -> bool:
    """Whether this register is an on/off indicator.

    Exactly two options, one of which is 0.  The second condition matters: the
    RVS software version (440) and the cooling release (143) also have two
    options, but neither is an on/off pair.
    """
    return (
        register.type is RegisterType.UINT16
        and register.options is not None
        and len(register.options) == 2
        and 0 in register.options
    )


def write_allowed(register: Register, write_level: WriteLevel | str) -> bool:
    """Whether this configuration may write this register.

    The register map's ``R/W`` is a statement about the hardware, not a licence
    to expose a control.  ``basic`` -- the default -- writes exactly the seven
    data points of DESIGN.md 10.1 and nothing else, so a user who wanted to
    adjust a setpoint cannot accidentally re-plumb the system.
    """
    level = WriteLevel(write_level)
    if not (register.writable or register.resettable):
        return False
    if register.expert_only:
        return level is WriteLevel.EXPERT
    if level is WriteLevel.BASIC:
        return register.writable and register.address in BASIC_WRITE_ADDRESSES
    return True


def is_reset_action(register: Register) -> bool:
    """Whether writing this register clears a counter instead of setting it."""
    return register.resettable or register.address in RESET_BY_WRITE_ADDRESSES


def control_for_write(register: Register) -> Control:
    """The kind of control a writable register gets."""
    if register.address in EXPERT_ACTIONS or is_reset_action(register):
        return Control.BUTTON
    if register.options is not None:
        return Control.SELECT
    return Control.NUMBER


def assign_controls(
    registers: Iterable[Register],
    *,
    write_level: WriteLevel | str = WriteLevel.BASIC,
    room_sensors: Iterable[str] = (),
) -> dict[str, frozenset[Control]]:
    """Decide which entities each register turns into.

    One register normally produces one entity.  The exceptions:

    * a counter that can be reset keeps its sensor *and* gains a button -- the
      runtime is worth reading whether or not you may clear it;
    * the registers a ``climate`` or ``water_heater`` entity is built from get
      that entity instead of a bare number or select, so there is never a second
      control for the same value.

    ``room_sensors`` names the circuits that actually have a room temperature
    sensor.  Without one, a climate entity would show a target temperature and
    no current temperature, which reads as broken; DESIGN.md 9.2 keeps the
    select and number pair for those circuits instead.
    """
    level = WriteLevel(write_level)
    by_address = {register.address: register for register in registers}
    controls: dict[str, set[Control]] = {}

    for register in by_address.values():
        kinds: set[Control] = set()
        if write_allowed(register, level):
            kind = control_for_write(register)
            kinds.add(kind)
            # A button is an action, not a display: the value stays a sensor.
            if kind is Control.BUTTON:
                kinds.add(
                    Control.BINARY_SENSOR if is_two_state(register) else Control.SENSOR
                )
        else:
            kinds.add(
                Control.BINARY_SENSOR if is_two_state(register) else Control.SENSOR
            )
        controls[register.key] = kinds

    def _claim(address: int, entity: Control) -> bool:
        """Hand a register over to a composite entity, if it is being written."""
        register = by_address.get(address)
        if register is None or not write_allowed(register, level):
            return False
        controls[register.key] = {entity}
        return True

    wanted_room_sensors = set(room_sensors)
    for circuit in HEATING_CIRCUITS:
        if circuit.block not in wanted_room_sensors:
            continue
        # Both halves have to be writable, or the entity would be half dead.
        mode = by_address.get(circuit.mode)
        comfort = by_address.get(circuit.comfort)
        if mode is None or comfort is None:
            continue
        if not (write_allowed(mode, level) and write_allowed(comfort, level)):
            continue
        _claim(circuit.mode, Control.CLIMATE)
        _claim(circuit.comfort, Control.CLIMATE)

    dhw = [by_address.get(DHW_MODE_ADDRESS), by_address.get(DHW_SETPOINT_ADDRESS)]
    if all(register is not None and write_allowed(register, level) for register in dhw):
        _claim(DHW_MODE_ADDRESS, Control.WATER_HEATER)
        _claim(DHW_SETPOINT_ADDRESS, Control.WATER_HEATER)

    return {key: frozenset(kinds) for key, kinds in controls.items()}


def registers_for(
    registers: Iterable[Register],
    controls: Mapping[str, frozenset[Control]],
    control: Control,
) -> tuple[Register, ...]:
    """Every register that produces an entity on one platform."""
    return tuple(
        register
        for register in registers
        if control in controls.get(register.key, frozenset())
    )


# ---------------------------------------------------------------------------
# Block presence heuristic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Which blocks to enable, which circuits have a room sensor, and why."""

    blocks: Mapping[str, bool]
    reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: Heating circuits whose room temperature register reads a real value.
    room_sensors: tuple[str, ...] = ()

    @property
    def enabled(self) -> tuple[str, ...]:
        """Block names that should get entities."""
        return tuple(name for name, present in self.blocks.items() if present)

    @property
    def excluded(self) -> tuple[str, ...]:
        """Block names discovery decided against."""
        return tuple(name for name, present in self.blocks.items() if not present)


def find_room_sensors(
    register_map: RegisterMap, rounds: Iterable[Mapping[str, DecodedValue]]
) -> tuple[str, ...]:
    """Heating circuits that report a plausible room temperature.

    A climate entity without a current temperature is misleading, so this is
    what decides whether one is created at all (DESIGN.md 9.2).  The answer is
    stored in the config entry rather than re-derived on every start, because a
    board that was only just powered up answers 0 for a few minutes and that
    would silently drop the entity on an unlucky restart.

    "Plausible" is doing real work here: a non-zero value is not enough.  A
    controller with no room unit connected reports a fixed out-of-range number
    rather than 0 -- 50.0 °C on the hardware this was written against -- and
    taking that at face value produces a thermostat that claims the living room
    is at 50 degrees.
    """
    rounds = list(rounds)
    minimum, maximum = ROOM_TEMPERATURE_RANGE
    found: list[str] = []
    for circuit in HEATING_CIRCUITS:
        register = register_map.at(circuit.room_temperature)
        if register is None:  # pragma: no cover - the map is closed
            continue
        for values in rounds:
            decoded = values.get(register.key)
            if decoded is None or decoded.disabled or decoded.value is None:
                continue
            if not isinstance(decoded.value, int | float):  # pragma: no cover
                continue
            if not minimum <= decoded.value <= maximum:
                _LOGGER.debug(
                    "%s room temperature reads %s, outside %s..%s: treating it as "
                    "no room sensor",
                    circuit.block,
                    decoded.value,
                    minimum,
                    maximum,
                )
                continue
            found.append(circuit.block)
            break
    return tuple(found)


def _block_is_alive(
    registers: Iterable[Register], rounds: Iterable[Mapping[str, DecodedValue]]
) -> tuple[bool, str]:
    """Whether a hydraulic block exists, and the register that decided it.

    The status register decides where a block has one.  Temperatures are not
    trustworthy on their own: a circuit that is not fitted still reports fixed
    placeholder values -- 50.0 °C for a missing room sensor, 140.0 °C for a
    missing flow setpoint -- and reading those as "not zero, therefore present"
    invents a whole hydraulic circuit out of nothing.  A status register that
    is absent answers ``---`` (code 0), which is the honest signal.

    Only read-only registers count either way: a setpoint holds whatever the
    controller was configured with whether or not the circuit is plumbed in.

    Two rounds are used because a single early read can legitimately answer 0
    while the MBIO is still fetching the value from the BSB bus (DESIGN.md
    section 5).
    """
    registers = list(registers)
    rounds = list(rounds)
    status = [
        register
        for register in registers
        if not register.writable and register.options_ref == "status_codes"
    ]
    indicators = status or [
        register
        for register in registers
        if not register.writable and register.type is RegisterType.TEMP
    ]
    if not indicators:
        # No usable signal -- assume present rather than silently hiding it.
        return True, "no status or temperature register to judge by"

    for values in rounds:
        for register in indicators:
            decoded = values.get(register.key)
            if decoded is None or decoded.disabled or not decoded.value:
                continue
            return True, f"register {register.address} reported {decoded.value}"

    kind = "status register" if status else "temperature register"
    return False, f"every {kind} read 0 or disabled in {len(rounds)} round(s)"


def analyse_blocks(
    register_map: RegisterMap, rounds: Iterable[Mapping[str, DecodedValue]]
) -> DiscoveryResult:
    """Decide which blocks are present from one or more full read rounds."""
    rounds = list(rounds)
    blocks: dict[str, bool] = {}
    reasons: dict[str, str] = {}

    for name in register_map.blocks:
        if name in ALWAYS_ON_BLOCKS:
            blocks[name] = True
            reasons[name] = "always enabled"
            continue
        if name not in DISCOVERABLE_BLOCKS:  # pragma: no cover - map is closed
            blocks[name] = True
            reasons[name] = "not part of the discovery heuristic"
            continue
        alive, reason = _block_is_alive(register_map.in_blocks([name]), rounds)
        blocks[name] = alive
        reasons[name] = reason

    return DiscoveryResult(
        blocks=MappingProxyType(blocks),
        reasons=MappingProxyType(reasons),
        room_sensors=find_room_sensors(register_map, rounds),
    )


async def async_run_discovery(
    client: MbioClient,
    register_map: RegisterMap,
    *,
    max_registers: int = 120,
    delay: float = SETUP_SECOND_READ_DELAY,
    rounds: int = 2,
) -> DiscoveryResult:
    """Read the whole map twice and decide which blocks exist.

    The first round also triggers the MBIO's BSB queries, so the second one is
    the trustworthy answer (DESIGN.md section 5).  A read failure is not fatal:
    discovery falls back to whatever rounds did succeed, and to "enable
    everything" if none did -- a wrong guess is recoverable from the options
    flow, a failed setup is not.
    """
    groups: tuple[ReadGroup, ...] = build_read_groups(
        register_map.registers,
        max_registers=max_registers,
        readable=register_map.addresses,
    )
    collected: list[Mapping[str, DecodedValue]] = []

    for round_number in range(rounds):
        if round_number:
            await asyncio.sleep(delay)
        values: dict[str, DecodedValue] = {}
        for group in groups:
            try:
                words = await client.async_read_group(group)
            except Exception as err:  # noqa: BLE001 - discovery must not hard fail
                _LOGGER.debug(
                    "Discovery could not read %s..%s: %s", group.start, group.end, err
                )
                continue
            values.update(group.decode(words))
        if values:
            collected.append(values)

    if not collected:
        _LOGGER.warning(
            "Discovery read nothing; enabling every block and leaving the choice "
            "to the options flow"
        )
        # Every block on, and no room sensor: a missing climate entity is a
        # smaller surprise than one that never shows a temperature.
        return DiscoveryResult(
            blocks=MappingProxyType({name: True for name in register_map.blocks}),
            reasons=MappingProxyType(
                {name: "discovery failed, enabled by default" for name in register_map.blocks}
            ),
        )

    result = analyse_blocks(register_map, collected)
    _LOGGER.debug("Discovery enabled %s, excluded %s", result.enabled, result.excluded)
    return result
