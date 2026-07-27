"""Tiered polling coordinators.

Four coordinators share one Modbus connection, one per tier, so a temperature
that the MBIO refreshes every 15 seconds is not held back by a maintenance
counter it refreshes every 255 (DESIGN.md section 8).

:class:`WaterstageRuntime` is what the platforms see: it owns the coordinators,
the register map and the discovery result, and it answers the one question every
entity has to ask -- is the BSB link up?
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .codec import DecodedValue, RegisterType
from .const import (
    LINK_STATUS_KEY,
    LINK_STATUS_OK,
    WARMUP_SECONDS,
    WRITE_OPTIMISTIC_TTL,
    WRITE_REREAD_DELAY,
    Control,
    Tier,
    WriteLevel,
)
from .discovery import DiscoveryResult, is_board_local, tier_for_register
from .hub import MbioClient, MbioError, ModbusGateway, ReadGroup, build_read_groups
from .registers import Register, RegisterMap

_LOGGER = logging.getLogger(__name__)

type CoordinatorData = dict[str, DecodedValue]


class MbioCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Reads one polling tier and decodes it into ``{register key: value}``."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: MbioClient,
        tier: Tier,
        registers: Iterable[Register],
        *,
        interval: int,
        max_registers: int = 120,
    ) -> None:
        self.tier = tier
        self.client = client
        self.registers: tuple[Register, ...] = tuple(registers)
        self.groups: tuple[ReadGroup, ...] = build_read_groups(
            self.registers, max_registers=max_registers
        )
        #: Groups that failed in the last cycle, for diagnostics.
        self.group_errors: dict[str, str] = {}
        #: Last raw response per group start address.  Kept so a bug report can
        #: show the words that came off the wire next to what they decoded to
        #: (DESIGN.md section 12); a few hundred integers in total.
        self.raw: dict[int, tuple[int, ...]] = {}

        # DESIGN.md section 5: exactly 0 from a temperature register during the
        # first minutes is more likely "not fetched yet" than 0 °C.  The
        # suppression is dropped per register as soon as a real value shows up.
        self._warmup_until = time.monotonic() + WARMUP_SECONDS
        self._settled: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{config_entry.title} {tier.value}",
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )

    @property
    def owns_link_status(self) -> bool:
        """Whether register 0 is read by this tier."""
        return any(register.is_link_status for register in self.registers)

    def group_for(self, register: Register) -> ReadGroup | None:
        """The read request that covers this register."""
        for group in self.groups:
            if group.start <= register.address and register.end_address <= group.end:
                return group
        return None

    async def async_refresh_group(self, group: ReadGroup) -> None:
        """Re-read one group and merge it into the tier's data.

        Used after a write, so a changed setpoint shows up in about two seconds
        instead of at the end of a five minute cycle -- without re-reading the
        other hundred registers of the tier (DESIGN.md section 10).
        """
        words = await self.client.async_read_group(group)
        self.raw[group.start] = words
        data = dict(self.data or {})
        data.update(group.decode(words))
        self.async_set_updated_data(self._apply_warmup(data))

    async def _async_update_data(self) -> CoordinatorData:
        """Read every group of this tier.

        A single failing group is tolerated -- the board may simply not
        implement an address, and the RS-485 bus is shared, so one bad answer
        should not blind the whole tier.  Only a complete failure is an update
        failure; registers of a failed group drop out of the data and their
        entities go unavailable, which is honest rather than stale.
        """
        values: CoordinatorData = {}
        errors: dict[str, str] = {}

        for group in self.groups:
            label = f"{group.start}..{group.end}"
            try:
                words = await self.client.async_read_group(group)
            except MbioError as err:
                errors[label] = str(err)
                _LOGGER.debug("%s: group %s failed: %s", self.name, label, err)
                continue
            self.raw[group.start] = words
            values.update(group.decode(words))

        self.group_errors = errors
        if errors and not values:
            raise UpdateFailed(
                f"no register group of the {self.tier.value} tier could be read: "
                + "; ".join(f"{label}: {error}" for label, error in errors.items())
            )
        if errors:
            _LOGGER.warning(
                "%s: %s of %s register groups failed (%s)",
                self.name,
                len(errors),
                len(self.groups),
                ", ".join(errors),
            )

        return self._apply_warmup(values)

    def _apply_warmup(self, values: CoordinatorData) -> CoordinatorData:
        """Hide the zeros a freshly powered board reports before it has data."""
        warming_up = time.monotonic() < self._warmup_until
        for register in self.registers:
            decoded = values.get(register.key)
            if decoded is None or decoded.disabled:
                continue
            if register.key in self._settled:
                continue
            if decoded.value:  # first real value -- stop second-guessing it
                self._settled.add(register.key)
                continue
            if warming_up and register.type is RegisterType.TEMP:
                values[register.key] = DecodedValue(None, decoded.disabled)
        return values


@dataclass(slots=True)
class WaterstageRuntime:
    """Everything a platform needs, hung off ``entry.runtime_data``."""

    gateway: ModbusGateway
    client: MbioClient
    register_map: RegisterMap
    registers: tuple[Register, ...]
    coordinators: Mapping[Tier, MbioCoordinator]
    discovery: DiscoveryResult
    write_level: WriteLevel
    controls: Mapping[str, frozenset[Control]] = field(default_factory=dict)
    board_info: Mapping[int, int] = field(default_factory=dict)
    _last_link: int | None = field(default=None, init=False, repr=False)
    #: Values written but not yet confirmed by a read: key -> (value, expiry).
    _optimistic: dict[str, tuple[DecodedValue, float]] = field(
        default_factory=dict, init=False, repr=False
    )

    def coordinator_for(self, register: Register) -> MbioCoordinator | None:
        """The coordinator that reads this register, if it is being read."""
        return self.coordinators.get(tier_for_register(register))

    def decoded(self, register: Register) -> DecodedValue | None:
        """The last decoded value of a register, or ``None`` if it has none.

        A value that was just written wins until the targeted re-read confirms
        it, so a slider does not snap back to the old reading for two seconds.
        """
        pending = self._optimistic.get(register.key)
        if pending is not None:
            value, expires = pending
            if time.monotonic() < expires:
                return value
            del self._optimistic[register.key]

        coordinator = self.coordinator_for(register)
        if coordinator is None or coordinator.data is None:
            return None
        return coordinator.data.get(register.key)

    @property
    def link_coordinator(self) -> MbioCoordinator | None:
        """The coordinator that reads register 0."""
        for coordinator in self.coordinators.values():
            if coordinator.owns_link_status:
                return coordinator
        return None

    @property
    def link_ok(self) -> bool:
        """Whether the MBIO can currently talk to the RVS21 over BSB.

        Unknown counts as up: on the very first refresh the answer is not in
        yet, and hiding every entity for one cycle would be worse than showing
        a value that turns out to be stale.  A board that does not answer Modbus
        at all is a different failure -- that one shows up as
        ``last_update_success`` being false on the coordinator itself.
        """
        coordinator = self.link_coordinator
        if coordinator is None or coordinator.data is None:
            return True
        decoded = coordinator.data.get(LINK_STATUS_KEY)
        if decoded is None or decoded.value is None:
            return True
        return decoded.value == LINK_STATUS_OK

    def register_is_available(self, register: Register) -> bool:
        """Apply every availability rule of DESIGN.md sections 3 and 4."""
        coordinator = self.coordinator_for(register)
        if coordinator is None or not coordinator.last_update_success:
            return False
        if coordinator.data is None:
            return False
        decoded = coordinator.data.get(register.key)
        if decoded is None or decoded.disabled:
            return False
        # A BSB outage invalidates everything the RVS21 produced, but not what
        # the board measures or counts itself.
        return self.link_ok or is_board_local(register)

    # -- writing ----------------------------------------------------------

    async def async_write(self, register: Register, value: Any) -> None:
        """Validate, write, and schedule the confirming read.

        Raises :class:`ServiceValidationError` for a value the controller would
        reject and :class:`HomeAssistantError` if the write itself fails; in
        both cases the previously read state stays in place.
        """
        try:
            register.validate(value)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        try:
            # One write, one function code.  The /O disable bit is never set:
            # a parameter is disabled from the controller's own menu.
            await self.client.async_write_register(register, value)
        except MbioError as err:
            raise HomeAssistantError(
                f"Writing {register.name} (register {register.address}) failed: {err}"
            ) from err

        self.async_set_optimistic(register, value)
        self.async_schedule_confirm(register)

    async def async_press(self, register: Register, value: int) -> None:
        """Write an action value: a counter reset, a trigger, a restart."""
        try:
            await self.client.gateway.async_write_register(
                register.address, value, slave=self.client.slave_id
            )
        except MbioError as err:
            raise HomeAssistantError(
                f"Writing {register.name} (register {register.address}) failed: {err}"
            ) from err
        self.async_schedule_confirm(register)

    @callback
    def async_set_optimistic(self, register: Register, value: Any) -> None:
        """Show a written value until a read confirms or contradicts it."""
        self._optimistic[register.key] = (
            DecodedValue(value, False),
            time.monotonic() + WRITE_OPTIMISTIC_TTL,
        )

    @callback
    def async_schedule_confirm(self, register: Register) -> None:
        """Re-read only the group the written register belongs to."""
        coordinator = self.coordinator_for(register)
        if coordinator is None:
            return
        group = coordinator.group_for(register)
        if group is None:  # pragma: no cover - every register is in a group
            return

        async def _confirm() -> None:
            await asyncio.sleep(WRITE_REREAD_DELAY)
            try:
                await coordinator.async_refresh_group(group)
            except MbioError as err:
                _LOGGER.debug("Could not confirm the write to %s: %s", register.key, err)
            finally:
                for member in group.registers:
                    self._optimistic.pop(member.key, None)
                coordinator.async_update_listeners()

        coordinator.config_entry.async_create_background_task(
            coordinator.hass, _confirm(), f"waterstage confirm {register.key}"
        )

    @callback
    def async_notify_link_change(self) -> None:
        """Re-evaluate the slower tiers when the BSB link flips.

        Their values are only refreshed every few minutes, but their
        availability changes the moment register 0 does.
        """
        link = self.link_coordinator
        for coordinator in self.coordinators.values():
            if coordinator is not link:
                coordinator.async_update_listeners()


def async_link_watcher(runtime: WaterstageRuntime) -> Callable[[], None]:
    """Listener that propagates register 0 changes to the other tiers."""

    @callback
    def _handle_update() -> None:
        coordinator = runtime.link_coordinator
        if coordinator is None or coordinator.data is None:
            return
        decoded = coordinator.data.get(LINK_STATUS_KEY)
        link = None if decoded is None else decoded.value
        if link != runtime._last_link:  # noqa: SLF001 - same package, one owner
            if runtime._last_link is not None:  # noqa: SLF001
                _LOGGER.info(
                    "BSB link status changed to %s; RVS21 entities are %s",
                    link,
                    "available" if runtime.link_ok else "unavailable",
                )
            runtime._last_link = link  # noqa: SLF001
            runtime.async_notify_link_change()

    return _handle_update
