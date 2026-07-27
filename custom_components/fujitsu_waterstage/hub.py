"""Shared async Modbus transport for the FWS-MBIO-002.

The board is a Modbus **RTU** slave on RS-485, but what reaches Home Assistant
depends on the gateway in between, and the two kinds behave very differently on
the wire:

* a *transparent* gateway forwards the raw RTU frames, CRC and all -- framing
  ``rtu``;
* a *protocol converting* gateway speaks Modbus TCP with an MBAP header on the
  network side and does the RTU framing itself -- framing ``tcp``.

Sending the wrong one produces no answer at all, from any slave id and any
function code, which looks exactly like a dead bus.  So the framing is not
assumed: it is probed at setup alongside the function code and stored in the
config entry.

Three properties matter for a bus that is shared with other devices
(DESIGN.md section 8.4):

* one :class:`asyncio.Lock` **per (host, port)**, shared across config entries,
  so a cheap gateway that accepts only one or two TCP connections is not
  overrun;
* a pause between requests so other masters get a turn;
* patient retries -- a single timeout usually means bus contention, not a dead
  device.

This module deliberately imports nothing from Home Assistant so that
``tools/dump.py`` can drive the hardware without a HA installation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

try:  # pymodbus >= 3.7
    from pymodbus import FramerType

    _FRAMERS: Final = {"tcp": FramerType.SOCKET, "rtu": FramerType.RTU}
except ImportError:  # pragma: no cover - pymodbus 3.6.x
    from pymodbus.framer import Framer

    _FRAMERS = {"tcp": Framer.SOCKET, "rtu": Framer.RTU}

from .codec import DecodedValue
from .registers import Register

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTER_REQUEST_DELAY",
    "DEFAULT_MAX_GAP",
    "DEFAULT_PORT",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "FRAMING_RTU",
    "FRAMING_TCP",
    "FRAMINGS",
    "FUNCTION_READ_HOLDING",
    "FUNCTION_READ_INPUT",
    "MAX_REGISTERS_PER_READ",
    "MBIO_PRODUCT_CODE",
    "MbioClient",
    "MbioConnectionError",
    "MbioError",
    "MbioResponseError",
    "ModbusGateway",
    "ReadGroup",
    "build_read_groups",
    "async_get_gateway",
    "async_release_gateway",
]

DEFAULT_PORT: Final = 502
DEFAULT_TIMEOUT: Final = 5.0
DEFAULT_RETRIES: Final = 3
DEFAULT_INTER_REQUEST_DELAY: Final = 0.05
DEFAULT_BACKOFF: Final = 0.5

#: Read function codes.  Both map onto the same holding-register space; 0x03 is
#: the default because writes go there too.
FUNCTION_READ_HOLDING: Final = 0x03
FUNCTION_READ_INPUT: Final = 0x04

#: How the gateway frames what it sends over TCP.  ``tcp`` is the Modbus TCP
#: MBAP header, ``rtu`` is a raw RTU frame tunnelled over TCP.
FRAMING_TCP: Final = "tcp"
FRAMING_RTU: Final = "rtu"

#: Probed in this order.  Protocol converting gateways are the common case, and
#: Home Assistant's own ``modbus:`` platform calls the same thing ``type: tcp``.
FRAMINGS: Final = (FRAMING_TCP, FRAMING_RTU)

DEFAULT_FRAMING: Final = FRAMING_TCP

#: Registers per read request.  Below the Modbus RTU limit of 125.
MAX_REGISTERS_PER_READ: Final = 120

#: Largest run of unused addresses that may be swallowed into one request.
DEFAULT_MAX_GAP: Final = 8

#: Product code in register 9900 that identifies an MBIO board.
MBIO_PRODUCT_CODE: Final = 0x0401


class MbioError(Exception):
    """Base class for transport failures."""


class MbioConnectionError(MbioError):
    """The gateway or the board did not answer, even after retries."""


class MbioResponseError(MbioError):
    """The board answered with a Modbus exception or a malformed frame.

    This is a *valid* answer -- writing a read-only register, or reading an
    address the board does not implement -- so it is never retried.
    """


# ---------------------------------------------------------------------------
# Read groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadGroup:
    """A contiguous span of addresses fetched with one request."""

    start: int
    count: int
    registers: tuple[Register, ...]

    @property
    def end(self) -> int:
        """Last address covered by this group."""
        return self.start + self.count - 1

    def words_for(self, register: Register, words: Sequence[int]) -> tuple[int, ...]:
        """Slice one register's raw words out of this group's response."""
        offset = register.address - self.start
        if offset < 0 or offset + register.length > len(words):
            raise ValueError(f"{register.key} is not inside group {self.start}..{self.end}")
        return tuple(words[offset : offset + register.length])

    def decode(self, words: Sequence[int]) -> dict[str, DecodedValue]:
        """Decode every register of this group from one response."""
        if len(words) != self.count:
            raise ValueError(
                f"group {self.start}..{self.end} expects {self.count} words, "
                f"got {len(words)}"
            )
        return {
            register.key: register.decode(self.words_for(register, words))
            for register in self.registers
        }


def build_read_groups(
    registers: Iterable[Register],
    *,
    max_registers: int = MAX_REGISTERS_PER_READ,
    max_gap: int = DEFAULT_MAX_GAP,
) -> tuple[ReadGroup, ...]:
    """Pack registers into as few read requests as possible.

    Two rules are non-negotiable:

    * a request never asks for more than ``max_registers`` registers;
    * a ``uint32``/``dtime`` pair is never split across two requests -- groups
      always grow by whole data points, so both words stay together.

    ``max_gap`` bounds how many unused addresses may be pulled in just to merge
    two runs; the address space is sparse, and reading a 60-word hole is still
    cheaper than a second round trip, but reading a 2000-word one is not.
    """
    if max_registers < 1:
        raise ValueError("max_registers must be at least 1")

    ordered = sorted(registers, key=lambda register: (register.address, register.length))
    groups: list[ReadGroup] = []
    current: list[Register] = []

    def flush() -> None:
        if not current:
            return
        start = current[0].address
        end = max(register.end_address for register in current)
        groups.append(ReadGroup(start=start, count=end - start + 1, registers=tuple(current)))
        current.clear()

    for register in ordered:
        if register.length > max_registers:
            raise ValueError(
                f"{register.key} needs {register.length} registers, "
                f"more than the {max_registers} allowed per request"
            )
        if current:
            start = current[0].address
            end = max(r.end_address for r in current)
            gap = register.address - end - 1
            if gap > max_gap or register.end_address - start + 1 > max_registers:
                flush()
        current.append(register)
    flush()

    return tuple(groups)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _device_kwarg() -> str:
    """pymodbus renamed ``slave`` to ``device_id`` in 3.9."""
    parameters = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
    return "device_id" if "device_id" in parameters else "slave"


class ModbusGateway:
    """One TCP connection to one RS-485 gateway, shared by every slave on it."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        framing: str = DEFAULT_FRAMING,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        inter_request_delay: float = DEFAULT_INTER_REQUEST_DELAY,
        backoff: float = DEFAULT_BACKOFF,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.framing = framing
        self.timeout = timeout
        self.retries = max(1, retries)
        self.inter_request_delay = inter_request_delay
        self.backoff = backoff
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._users = 0

    def _default_client_factory(self) -> Any:
        # retries=1: this class owns the retry policy, so pymodbus must not add
        # a second, invisible one on top of it.
        try:
            framer = _FRAMERS[self.framing]
        except KeyError:
            raise ValueError(
                f"unknown framing {self.framing!r}, expected one of {FRAMINGS}"
            ) from None
        return AsyncModbusTcpClient(
            self.host,
            port=self.port,
            framer=framer,
            timeout=self.timeout,
            retries=1,
        )

    async def async_set_framing(self, framing: str) -> None:
        """Switch framing, dropping the connection so the next one uses it.

        A gateway has exactly one framing, so this is a property of the hardware
        rather than of a config entry: whoever discovers the right one fixes it
        for everyone sharing the connection.
        """
        if framing == self.framing:
            return
        if framing not in FRAMINGS:
            raise ValueError(f"unknown framing {framing!r}, expected one of {FRAMINGS}")
        _LOGGER.debug(
            "Switching %s:%s from %s to %s framing", self.host, self.port,
            self.framing, framing,
        )
        async with self._lock:
            self._disconnect()
            self.framing = framing

    @property
    def key(self) -> tuple[str, int]:
        """Registry key -- one gateway object per (host, port)."""
        return (self.host, self.port)

    @property
    def lock(self) -> asyncio.Lock:
        """The bus lock.  Every request on this gateway serialises through it."""
        return self._lock

    @property
    def connected(self) -> bool:
        """Whether the TCP socket is currently up."""
        return bool(self._client is not None and getattr(self._client, "connected", False))

    # -- lifecycle --------------------------------------------------------

    async def async_connect(self) -> None:
        """Open the TCP connection, raising :class:`MbioConnectionError`."""
        async with self._lock:
            await self._ensure_connected()

    async def async_close(self) -> None:
        """Close the TCP connection."""
        async with self._lock:
            self._disconnect()

    async def _ensure_connected(self) -> None:
        if self.connected:
            return
        if self._client is None:
            self._client = self._client_factory()
        try:
            connected = await self._client.connect()
        except (ModbusException, OSError) as err:
            self._disconnect()
            raise MbioConnectionError(f"cannot reach {self.host}:{self.port}: {err}") from err
        if not connected:
            self._disconnect()
            raise MbioConnectionError(f"cannot reach {self.host}:{self.port}")

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            _LOGGER.debug("Error while closing %s:%s", self.host, self.port, exc_info=True)

    # -- reference counting ----------------------------------------------

    def _acquire(self) -> None:
        self._users += 1

    async def _release(self) -> bool:
        """Drop one user; close and report ``True`` when the last one leaves."""
        self._users = max(0, self._users - 1)
        if self._users:
            return False
        await self.async_close()
        return True

    # -- requests ---------------------------------------------------------

    async def _pace(self) -> None:
        """Hold the bus idle for ``inter_request_delay`` between requests."""
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _execute(self, what: str, call: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run one Modbus transaction with locking, pacing and retries."""
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            async with self._lock:
                try:
                    await self._ensure_connected()
                    await self._pace()
                    response = await asyncio.wait_for(call(self._client), self.timeout)
                except (TimeoutError, ModbusException, OSError, MbioConnectionError) as err:
                    last_error = err
                    self._disconnect()
                    _LOGGER.debug(
                        "%s failed on %s:%s (attempt %s/%s): %s",
                        what,
                        self.host,
                        self.port,
                        attempt,
                        self.retries,
                        err,
                    )
                else:
                    self._next_request_at = time.monotonic() + self.inter_request_delay
                    if response.isError():
                        # A Modbus exception response is a real answer from the
                        # board; retrying it would only produce the same one.
                        raise MbioResponseError(f"{what} rejected by the board: {response}")
                    return response
            if attempt < self.retries:
                # Sleep outside the lock so other users of this gateway --
                # and the other devices on the RS-485 bus -- get a turn.
                await asyncio.sleep(self.backoff * 2 ** (attempt - 1))

        raise MbioConnectionError(
            f"{what} failed after {self.retries} attempts on {self.host}:{self.port}: "
            f"{last_error}"
        ) from last_error

    async def async_read_registers(
        self,
        address: int,
        count: int,
        *,
        slave: int,
        function_code: int = FUNCTION_READ_HOLDING,
    ) -> tuple[int, ...]:
        """Read ``count`` registers with FC 0x03 (default) or 0x04."""
        if count < 1 or count > MAX_REGISTERS_PER_READ:
            raise ValueError(f"count must be 1..{MAX_REGISTERS_PER_READ}, got {count}")
        if function_code == FUNCTION_READ_HOLDING:
            method = "read_holding_registers"
        elif function_code == FUNCTION_READ_INPUT:
            method = "read_input_registers"
        else:
            raise ValueError(f"unsupported read function code {function_code:#04x}")

        kwargs = {"count": count, _device_kwarg(): slave}
        response = await self._execute(
            f"read {address}..{address + count - 1}",
            lambda client: getattr(client, method)(address, **kwargs),
        )
        words = tuple(response.registers)
        if len(words) != count:
            raise MbioResponseError(
                f"read {address}..{address + count - 1} returned {len(words)} "
                f"registers instead of {count}"
            )
        return words

    async def async_write_register(self, address: int, value: int, *, slave: int) -> None:
        """Write a single register with FC 0x06."""
        kwargs = {_device_kwarg(): slave}
        await self._execute(
            f"write {address}",
            lambda client: client.write_register(address, value, **kwargs),
        )

    async def async_write_registers(
        self, address: int, values: Sequence[int], *, slave: int
    ) -> None:
        """Write consecutive registers with FC 0x10, high word first."""
        kwargs = {_device_kwarg(): slave}
        payload = list(values)
        await self._execute(
            f"write {address}..{address + len(payload) - 1}",
            lambda client: client.write_registers(address, payload, **kwargs),
        )


class MbioClient:
    """One MBIO board: a gateway plus a slave id and the read function code."""

    def __init__(
        self,
        gateway: ModbusGateway,
        slave_id: int,
        *,
        function_code: int = FUNCTION_READ_HOLDING,
    ) -> None:
        self.gateway = gateway
        self.slave_id = slave_id
        self.function_code = function_code

    async def async_read(self, address: int, count: int) -> tuple[int, ...]:
        """Read a raw address span."""
        return await self.gateway.async_read_registers(
            address, count, slave=self.slave_id, function_code=self.function_code
        )

    async def async_probe(self, address: int = 9900) -> tuple[str, int]:
        """Find the framing and function code this gateway answers to.

        Both function codes map onto the same holding-register space, but not
        every gateway or firmware honours both, and the framing depends on
        whether the gateway forwards raw RTU or converts to Modbus TCP.  Neither
        can be assumed, so both are probed and the working pair goes into the
        config entry (DESIGN.md section 1).

        The two failure modes are worth telling apart, and the exception type
        does it: a Modbus exception response means the board is listening and
        the framing is right, just not that function code.  Silence on every
        combination usually means something else entirely -- wrong slave id,
        or another master holding the gateway's only connection.
        """
        errors: list[str] = []
        answered = False
        for framing in FRAMINGS:
            await self.gateway.async_set_framing(framing)
            for code in (FUNCTION_READ_HOLDING, FUNCTION_READ_INPUT):
                try:
                    await self.gateway.async_read_registers(
                        address, 1, slave=self.slave_id, function_code=code
                    )
                except MbioResponseError as err:
                    answered = True
                    errors.append(f"{framing}/{code:#04x}: {err}")
                except MbioError as err:
                    errors.append(f"{framing}/{code:#04x}: {err}")
                else:
                    self.function_code = code
                    return framing, code

        detail = "; ".join(errors)
        if answered:
            raise MbioConnectionError(
                f"slave {self.slave_id} rejected every read function code ({detail})"
            )
        raise MbioConnectionError(
            f"slave {self.slave_id} did not answer with either framing. Check the "
            f"slave id, and that no other master is holding the gateway's "
            f"connection ({detail})"
        )

    async def async_read_group(self, group: ReadGroup) -> tuple[int, ...]:
        """Read the raw words of one :class:`ReadGroup`."""
        return await self.async_read(group.start, group.count)

    async def async_read_groups(
        self, groups: Iterable[ReadGroup]
    ) -> dict[str, DecodedValue]:
        """Read and decode several groups sequentially."""
        values: dict[str, DecodedValue] = {}
        for group in groups:
            values.update(group.decode(await self.async_read_group(group)))
        return values

    async def async_read_register(self, register: Register) -> DecodedValue:
        """Read and decode a single data point."""
        return register.decode(await self.async_read(register.address, register.length))

    async def async_write_register(self, register: Register, value: Any) -> None:
        """Encode and write a data point: FC 0x06 for one word, 0x10 for two.

        The ``/O`` disable bit is never set -- a parameter is disabled from the
        controller's menu, not over Modbus.
        """
        if not register.writable:
            raise MbioResponseError(f"{register.key} is not writable ({register.access})")
        words = register.encode(value)
        if len(words) == 1:
            await self.gateway.async_write_register(
                register.address, words[0], slave=self.slave_id
            )
        else:
            await self.gateway.async_write_registers(
                register.address, words, slave=self.slave_id
            )

    async def async_reset_register(self, register: Register) -> None:
        """Clear an ``R/Reset`` counter -- any value resets it, so write 0."""
        if not register.resettable:
            raise MbioResponseError(f"{register.key} is not resettable ({register.access})")
        await self.gateway.async_write_register(register.address, 0, slave=self.slave_id)


# ---------------------------------------------------------------------------
# Gateway registry -- one connection per (host, port), across config entries
# ---------------------------------------------------------------------------

_GATEWAYS: dict[tuple[str, int], ModbusGateway] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def async_get_gateway(
    host: str,
    port: int = DEFAULT_PORT,
    *,
    registry: dict[tuple[str, int], ModbusGateway] | None = None,
    **kwargs: Any,
) -> ModbusGateway:
    """Return the shared gateway for ``(host, port)``, creating it if needed.

    A second config entry on the same gateway reuses the existing connection;
    its timeout/retry settings are ignored, because the connection is shared.
    """
    gateways = _GATEWAYS if registry is None else registry
    async with _REGISTRY_LOCK:
        gateway = gateways.get((host, port))
        if gateway is None:
            gateway = ModbusGateway(host, port, **kwargs)
            gateways[(host, port)] = gateway
        elif kwargs:
            _LOGGER.debug(
                "Reusing the existing connection to %s:%s; its transport settings win",
                host,
                port,
            )
        gateway._acquire()  # noqa: SLF001 - the registry owns the reference count
        return gateway


async def async_release_gateway(
    gateway: ModbusGateway,
    *,
    registry: dict[tuple[str, int], ModbusGateway] | None = None,
) -> None:
    """Drop one user of a gateway, closing it when the last one is gone."""
    gateways = _GATEWAYS if registry is None else registry
    async with _REGISTRY_LOCK:
        if await gateway._release():  # noqa: SLF001 - see above
            gateways.pop(gateway.key, None)
