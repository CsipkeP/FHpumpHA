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
    BOARD_LOCAL_ADDRESSES,
    DISCOVERABLE_BLOCKS,
    INTERFACE_DIAGNOSTIC_ADDRESSES,
    SETUP_SECOND_READ_DELAY,
    Tier,
    WriteLevel,
)
from .hub import MbioClient, ReadGroup, build_read_groups
from .registers import Register, RegisterMap

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DiscoveryResult",
    "analyse_blocks",
    "async_run_discovery",
    "is_board_local",
    "select_registers",
    "tier_for_register",
    "tier_registers",
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
# Block presence heuristic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Which blocks to enable, and why."""

    blocks: Mapping[str, bool]
    reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def enabled(self) -> tuple[str, ...]:
        """Block names that should get entities."""
        return tuple(name for name, present in self.blocks.items() if present)

    @property
    def excluded(self) -> tuple[str, ...]:
        """Block names discovery decided against."""
        return tuple(name for name, present in self.blocks.items() if not present)


def _block_is_alive(
    registers: Iterable[Register], rounds: Iterable[Mapping[str, DecodedValue]]
) -> bool:
    """True when any status or measured temperature register carries a value.

    A block counts as present when at least one of its indicator registers is
    neither zero nor flagged disabled, in at least one of the read rounds.  Two
    rounds are used because a single early read can legitimately answer 0 while
    the MBIO is still fetching the value from the BSB bus (DESIGN.md section 5).

    Only read-only registers count.  A setpoint keeps whatever the controller
    was configured with whether or not the circuit is plumbed in, so including
    them would report every block as present.
    """
    indicators = [
        register
        for register in registers
        if not register.writable
        and (
            register.type is RegisterType.TEMP
            or register.options_ref == "status_codes"
        )
    ]
    if not indicators:
        # No usable signal -- assume present rather than silently hiding it.
        return True
    for values in rounds:
        for register in indicators:
            decoded = values.get(register.key)
            if decoded is None or decoded.disabled or decoded.value is None:
                continue
            if decoded.value != 0:
                return True
    return False


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
        alive = _block_is_alive(register_map.in_blocks([name]), rounds)
        blocks[name] = alive
        reasons[name] = (
            "a status or temperature register reported a value"
            if alive
            else f"every indicator register read 0 or disabled in {len(rounds)} round(s)"
        )

    return DiscoveryResult(
        blocks=MappingProxyType(blocks), reasons=MappingProxyType(reasons)
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
        register_map.registers, max_registers=max_registers
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
        return DiscoveryResult(
            blocks=MappingProxyType({name: True for name in register_map.blocks}),
            reasons=MappingProxyType(
                {name: "discovery failed, enabled by default" for name in register_map.blocks}
            ),
        )

    result = analyse_blocks(register_map, collected)
    _LOGGER.debug("Discovery enabled %s, excluded %s", result.enabled, result.excluded)
    return result
