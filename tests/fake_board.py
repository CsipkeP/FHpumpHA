"""A stand-in for the pymodbus client, so tests can drive a whole board.

The real :class:`~fujitsu_waterstage.hub.ModbusGateway` sits on top of this, so
locking, pacing, retries and the read-group logic are all exercised for real --
only the socket is fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A plausible, mostly idle installation: the BSB link is up, the compressor is
#: running, HC1 is in automatic and nothing else is fitted.
DEFAULT_WORDS: dict[int, int] = {
    0: 1,  # BSB link OK
    1: 137,  # status: Heating mode
    2: 255,  # compressor 1 on
    7: 250,  # return 25.0 °C
    8: 300,  # setpoint 30.0 °C
    9: 340,  # flow 34.0 °C
    10: 45,  # modulation 45 %
    13: 0x4065,  # heat exchanger 10.1 °C, flagged disabled
    17: 0xFF9B,  # outside -10.1 °C
    23: 35,  # 3.5 starts per hour run
    24: 0x0001,
    25: 0x2345,  # compressor runtime 74565 s
    40: 1,  # DHW on
    41: 500,  # DHW setpoint 50.0 °C
    60: 137,
    63: 480,  # DHW actual 48.0 °C
    100: 1,  # HC1 automatic
    101: 210,  # comfort 21.0 °C
    105: 250,  # slope 2.5
    120: 137,
    121: 255,  # HC1 pump running
    124: 215,  # room 21.5 °C
    128: 1,  # room thermostat: demand
    400: 0,
    401: 105,  # fault history 1: maintenance message
    412: 0x07B4,
    413: 0x5AD4,  # 2023-04-11 11:20
    440: 85,  # RVS V8.5
    450: 255,  # QX1 on
    9900: 0x0401,  # MBIO product code
    9901: 0x0201,
    9902: 0x1704,
    9903: 0x0042,
    9905: 960,  # 9600 baud
}


class FakeResponse:
    """Minimal pymodbus response."""

    def __init__(self, registers: list[int] | None = None, error: bool = False) -> None:
        self.registers = registers or []
        self._error = error

    def isError(self) -> bool:  # noqa: N802 - pymodbus spelling
        return self._error


@dataclass
class FakeClient:
    """Stand-in for ``AsyncModbusTcpClient``."""

    words: dict[int, int] = field(default_factory=dict)
    connected: bool = False
    fail_reads: int = 0
    read_error_response: bool = False
    connect_failures: int = 0
    calls: list[tuple[str, Any, Any]] = field(default_factory=list)
    connects: int = 0
    #: Addresses the board refuses to answer, as a real one does for an
    #: unimplemented register.
    rejected: set[int] = field(default_factory=set)

    async def connect(self) -> bool:
        self.connects += 1
        if self.connect_failures > 0:
            self.connect_failures -= 1
            return False
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(
        self, address: int, *, count: int = 1, **kwargs: Any
    ) -> FakeResponse:
        self.calls.append(("read", address, count))
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise TimeoutError("bus busy")
        if self.read_error_response:
            return FakeResponse(error=True)
        if self.rejected & set(range(address, address + count)):
            return FakeResponse(error=True)
        return FakeResponse([self.words.get(address + i, 0) for i in range(count)])

    async def read_input_registers(
        self, address: int, *, count: int = 1, **kwargs: Any
    ) -> FakeResponse:
        return await self.read_holding_registers(address, count=count, **kwargs)

    async def write_register(self, address: int, value: int, **kwargs: Any) -> FakeResponse:
        self.calls.append(("write", address, value))
        self.words[address] = value
        return FakeResponse()

    async def write_registers(
        self, address: int, values: list[int], **kwargs: Any
    ) -> FakeResponse:
        self.calls.append(("write_multi", address, list(values)))
        for offset, value in enumerate(values):
            self.words[address + offset] = value
        return FakeResponse()


def make_board(**overrides: int) -> FakeClient:
    """A fake client preloaded with a plausible installation."""
    words = dict(DEFAULT_WORDS)
    words.update(overrides)
    return FakeClient(words=words)
