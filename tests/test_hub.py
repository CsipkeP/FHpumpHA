"""Transport tests: read grouping, locking, pacing, retries.

No hardware and no sockets -- a fake pymodbus client stands in for the gateway.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fujitsu_waterstage.codec import RegisterType
from fujitsu_waterstage.hub import (
    DEFAULT_MAX_GAP,
    FRAMING_RTU,
    FRAMING_TCP,
    FUNCTION_READ_HOLDING,
    FUNCTION_READ_INPUT,
    MAX_REGISTERS_PER_READ,
    MbioClient,
    MbioConnectionError,
    MbioResponseError,
    ModbusGateway,
    ReadGroup,
    async_get_gateway,
    async_release_gateway,
    build_read_groups,
)
from fujitsu_waterstage.registers import Register, RegisterMap

from .fake_board import FakeClient, FakeResponse

# ---------------------------------------------------------------------------
# Read groups
# ---------------------------------------------------------------------------


def _register(address: int, register_type: RegisterType = RegisterType.UINT16) -> Register:
    return Register(
        key=f"test_{address}",
        address=address,
        type=register_type,
        access="R",
        block="test",
        name=f"Test {address}",
        length=register_type.length,
    )


class TestReadGroups:
    def test_contiguous_run_becomes_one_group(self) -> None:
        groups = build_read_groups(_register(a) for a in range(10))
        assert len(groups) == 1
        assert (groups[0].start, groups[0].count) == (0, 10)

    def test_large_gap_splits(self) -> None:
        groups = build_read_groups([_register(0), _register(1), _register(500)])
        assert [(g.start, g.count) for g in groups] == [(0, 2), (500, 1)]

    def test_small_gap_is_merged(self) -> None:
        groups = build_read_groups([_register(0), _register(1 + DEFAULT_MAX_GAP)])
        assert len(groups) == 1
        assert groups[0].count == DEFAULT_MAX_GAP + 2

    def test_gap_one_larger_splits(self) -> None:
        groups = build_read_groups([_register(0), _register(2 + DEFAULT_MAX_GAP)])
        assert len(groups) == 2

    def test_never_exceeds_the_request_limit(self) -> None:
        groups = build_read_groups(
            (_register(a) for a in range(500)), max_registers=MAX_REGISTERS_PER_READ
        )
        assert all(group.count <= MAX_REGISTERS_PER_READ for group in groups)
        assert sum(len(group.registers) for group in groups) == 500

    def test_multi_register_pair_is_never_split(self) -> None:
        """A uint32/dtime pair must land whole inside one request."""
        registers = [_register(a) for a in range(9)]
        registers.append(_register(9, RegisterType.UINT32))  # occupies 9 and 10
        groups = build_read_groups(registers, max_registers=10)
        pair = next(r for r in registers if r.length == 2)
        owner = [g for g in groups if pair in g.registers]
        assert len(owner) == 1
        assert owner[0].start <= pair.address
        assert owner[0].end >= pair.end_address

    def test_real_map_pairs_are_never_split(self, register_map: RegisterMap) -> None:
        for max_registers in (2, 8, 16, 64, 120):
            groups = build_read_groups(register_map.registers, max_registers=max_registers)
            covered = {r.key for group in groups for r in group.registers}
            assert covered == set(register_map.keys)
            for group in groups:
                assert group.count <= max_registers
                for register in group.registers:
                    assert group.start <= register.address
                    assert register.end_address <= group.end

    def test_real_map_group_count_is_sane(self, register_map: RegisterMap) -> None:
        groups = build_read_groups(register_map.registers)
        assert 1 < len(groups) <= 20
        assert all(group.count <= MAX_REGISTERS_PER_READ for group in groups)

    def test_register_larger_than_the_limit(self) -> None:
        with pytest.raises(ValueError):
            build_read_groups([_register(0, RegisterType.UINT32)], max_registers=1)

    def test_zero_limit(self) -> None:
        with pytest.raises(ValueError):
            build_read_groups([_register(0)], max_registers=0)

    def test_empty(self) -> None:
        assert build_read_groups([]) == ()

    def test_unsorted_input(self) -> None:
        groups = build_read_groups([_register(2), _register(0), _register(1)])
        assert [(g.start, g.count) for g in groups] == [(0, 3)]

    def test_decode_slices_the_response(self, register_map: RegisterMap) -> None:
        registers = [register_map.at(a) for a in (7, 8, 9)]
        group = build_read_groups(registers)[0]
        assert (group.start, group.count) == (7, 3)
        decoded = group.decode([0x00FA, 0x00C8, 0x0154])
        assert decoded["heat_pump_return_temperature"].value == 25.0
        assert decoded["heat_pump_temperature_setpoint"].value == 20.0
        assert decoded["heat_pump_flow_temperature"].value == 34.0

    def test_decode_of_a_pair(self, register_map: RegisterMap) -> None:
        group = build_read_groups([register_map.at(412)])[0]
        assert group.count == 2
        decoded = group.decode([0x07B4, 0x5AD4])
        assert decoded["faults_fault_history_1_date_time"].value.year == 2023

    def test_decode_rejects_a_short_response(self, register_map: RegisterMap) -> None:
        group = build_read_groups([register_map.at(412)])[0]
        with pytest.raises(ValueError):
            group.decode([0x07B4])

    def test_words_for_outside_group(self, register_map: RegisterMap) -> None:
        group = ReadGroup(start=7, count=1, registers=(register_map.at(7),))
        with pytest.raises(ValueError):
            group.words_for(register_map.at(9), [0])


def _gateway(client: FakeClient, **kwargs: Any) -> ModbusGateway:
    kwargs.setdefault("inter_request_delay", 0.0)
    kwargs.setdefault("backoff", 0.0)
    return ModbusGateway("gateway.test", 502, client_factory=lambda: client, **kwargs)


def _set_framing(client: FakeClient, gateway: ModbusGateway) -> FakeClient:
    """Client factory that tells the fake which framing it was built with."""
    client.requested_framing = gateway.framing
    return client


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class TestGateway:
    pytestmark = pytest.mark.asyncio

    async def test_read(self) -> None:
        client = FakeClient(words={7: 250, 8: 200})
        gateway = _gateway(client)
        assert await gateway.async_read_registers(7, 2, slave=3) == (250, 200)
        assert client.calls == [("read", 7, 2)]
        await gateway.async_close()

    async def test_read_function_code_selection(self) -> None:
        client = FakeClient()
        gateway = _gateway(client)
        await gateway.async_read_registers(0, 1, slave=3, function_code=FUNCTION_READ_INPUT)
        with pytest.raises(ValueError):
            await gateway.async_read_registers(0, 1, slave=3, function_code=0x17)

    async def test_read_count_limit(self) -> None:
        gateway = _gateway(FakeClient())
        with pytest.raises(ValueError):
            await gateway.async_read_registers(0, MAX_REGISTERS_PER_READ + 1, slave=3)
        with pytest.raises(ValueError):
            await gateway.async_read_registers(0, 0, slave=3)

    async def test_write_single_and_multiple(self) -> None:
        client = FakeClient()
        gateway = _gateway(client)
        await gateway.async_write_register(41, 500, slave=3)
        await gateway.async_write_registers(24, [1, 2], slave=3)
        assert client.calls == [("write", 41, 500), ("write_multi", 24, [1, 2])]

    async def test_retries_then_succeeds(self) -> None:
        client = FakeClient(words={7: 250}, fail_reads=2)
        gateway = _gateway(client, retries=3)
        assert await gateway.async_read_registers(7, 1, slave=3) == (250,)
        assert len(client.calls) == 3

    async def test_gives_up_after_the_retry_budget(self) -> None:
        client = FakeClient(fail_reads=99)
        gateway = _gateway(client, retries=3)
        with pytest.raises(MbioConnectionError):
            await gateway.async_read_registers(7, 1, slave=3)
        assert len(client.calls) == 3

    async def test_backoff_is_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeClient(fail_reads=99)
        gateway = _gateway(client, retries=4, backoff=0.01)
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def record(delay: float, *args: Any, **kwargs: Any) -> None:
            slept.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", record)
        with pytest.raises(MbioConnectionError):
            await gateway.async_read_registers(7, 1, slave=3)
        # Three waits between four attempts, and no wait after the last one.
        assert slept == [0.01, 0.02, 0.04]

    async def test_modbus_exception_response_is_not_retried(self) -> None:
        """A rejection is a real answer -- asking again only repeats it."""
        client = FakeClient(read_error_response=True)
        gateway = _gateway(client, retries=3)
        with pytest.raises(MbioResponseError):
            await gateway.async_read_registers(7, 1, slave=3)
        assert len(client.calls) == 1

    async def test_short_response_is_rejected(self) -> None:
        class ShortClient(FakeClient):
            async def read_holding_registers(self, address: int, *, count: int = 1, **kw: Any):
                return FakeResponse([0])

        gateway = _gateway(ShortClient())
        with pytest.raises(MbioResponseError):
            await gateway.async_read_registers(7, 4, slave=3)

    async def test_connect_failure(self) -> None:
        client = FakeClient(connect_failures=99)
        gateway = _gateway(client, retries=2)
        with pytest.raises(MbioConnectionError):
            await gateway.async_connect()

    async def test_reconnects_after_a_failure(self) -> None:
        client = FakeClient(words={7: 1}, fail_reads=1)
        gateway = _gateway(client, retries=2)
        await gateway.async_read_registers(7, 1, slave=3)
        assert client.connects == 2  # the failed attempt dropped the socket

    async def test_requests_are_serialised(self) -> None:
        """One lock per gateway: the shared RS-485 bus carries one frame at a time."""
        overlap = 0
        active = 0

        class SlowClient(FakeClient):
            async def read_holding_registers(self, address: int, *, count: int = 1, **kw: Any):
                nonlocal overlap, active
                active += 1
                overlap = max(overlap, active)
                await asyncio.sleep(0.01)
                active -= 1
                return FakeResponse([0] * count)

        gateway = _gateway(SlowClient())
        await asyncio.gather(
            *(gateway.async_read_registers(address, 1, slave=3) for address in range(5))
        )
        assert overlap == 1

    async def test_inter_request_delay(self) -> None:
        client = FakeClient()
        gateway = _gateway(client, inter_request_delay=0.05)
        start = asyncio.get_running_loop().time()
        for _ in range(3):
            await gateway.async_read_registers(0, 1, slave=3)
        # The first request is free; the next two each wait out the delay.
        assert asyncio.get_running_loop().time() - start >= 0.09


class TestGatewayRegistry:
    pytestmark = pytest.mark.asyncio

    async def test_one_gateway_per_host_and_port(self) -> None:
        registry: dict = {}
        first = await async_get_gateway("10.0.0.1", 502, registry=registry)
        second = await async_get_gateway("10.0.0.1", 502, registry=registry)
        other = await async_get_gateway("10.0.0.2", 502, registry=registry)
        assert first is second
        assert first is not other
        assert first.lock is second.lock
        assert len(registry) == 2

    async def test_refcounted_release(self) -> None:
        registry: dict = {}
        gateway = await async_get_gateway("10.0.0.1", 502, registry=registry)
        await async_get_gateway("10.0.0.1", 502, registry=registry)
        await async_release_gateway(gateway, registry=registry)
        assert registry  # a second user is still holding it
        await async_release_gateway(gateway, registry=registry)
        assert not registry


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestMbioClient:
    pytestmark = pytest.mark.asyncio

    async def test_read_register(self, register_map: RegisterMap) -> None:
        client = MbioClient(_gateway(FakeClient(words={7: 250})), slave_id=3)
        assert await client.async_read_register(register_map.at(7)) == (25.0, False)

    async def test_read_groups(self, register_map: RegisterMap) -> None:
        fake = FakeClient(words={7: 250, 8: 200, 9: 340})
        client = MbioClient(_gateway(fake), slave_id=3)
        groups = build_read_groups([register_map.at(a) for a in (7, 8, 9)])
        values = await client.async_read_groups(groups)
        assert values["heat_pump_flow_temperature"].value == 34.0

    async def test_write_single_register_uses_fc06(
        self, register_map: RegisterMap
    ) -> None:
        fake = FakeClient()
        client = MbioClient(_gateway(fake), slave_id=3)
        await client.async_write_register(register_map.at(41), 50.0)
        assert fake.calls == [("write", 41, 500)]

    async def test_write_rejects_a_read_only_register(
        self, register_map: RegisterMap
    ) -> None:
        client = MbioClient(_gateway(FakeClient()), slave_id=3)
        with pytest.raises(MbioResponseError):
            await client.async_write_register(register_map.at(7), 20.0)

    async def test_reset_writes_zero(self, register_map: RegisterMap) -> None:
        fake = FakeClient()
        client = MbioClient(_gateway(fake), slave_id=3)
        await client.async_reset_register(register_map.at(18))
        assert fake.calls == [("write", 18, 0)]

    async def test_reset_rejects_a_plain_register(self, register_map: RegisterMap) -> None:
        client = MbioClient(_gateway(FakeClient()), slave_id=3)
        with pytest.raises(MbioResponseError):
            await client.async_reset_register(register_map.at(41))

    async def test_probe_prefers_function_code_3(self) -> None:
        client = MbioClient(_gateway(FakeClient(words={9900: 0x0401})), slave_id=3)
        assert await client.async_probe() == (FRAMING_TCP, FUNCTION_READ_HOLDING)

    async def test_probe_falls_back_to_function_code_4(self) -> None:
        class HoldingRejected(FakeClient):
            async def read_holding_registers(self, address: int, *, count: int = 1, **kw: Any):
                self.calls.append(("read03", address, count))
                return FakeResponse(error=True)

            async def read_input_registers(self, address: int, *, count: int = 1, **kw: Any):
                self.calls.append(("read04", address, count))
                return FakeResponse([0x0401])

        client = MbioClient(_gateway(HoldingRejected()), slave_id=3)
        assert await client.async_probe() == (FRAMING_TCP, FUNCTION_READ_INPUT)
        assert client.function_code == FUNCTION_READ_INPUT

    async def test_probe_failure(self) -> None:
        client = MbioClient(_gateway(FakeClient(fail_reads=99), retries=1), slave_id=3)
        with pytest.raises(
            MbioConnectionError, match="did not answer with either framing"
        ):
            await client.async_probe()

    async def test_probe_finds_the_framing(self) -> None:
        """A protocol converting gateway is silent to raw RTU, and the reverse."""
        fake = FakeClient(words={9900: 0x0401}, framing=FRAMING_RTU)
        gateway = _gateway(fake, retries=1, framing=FRAMING_TCP)
        gateway._client_factory = lambda: _set_framing(fake, gateway)  # noqa: SLF001
        client = MbioClient(gateway, slave_id=3)

        assert await client.async_probe() == (FRAMING_RTU, FUNCTION_READ_HOLDING)
        assert gateway.framing == FRAMING_RTU

    async def test_a_framing_mismatch_names_the_likely_causes(self) -> None:
        """This failure looks like a dead bus, so the message has to point somewhere."""
        fake = FakeClient(words={9900: 0x0401}, framing="none")
        gateway = _gateway(fake, retries=1)
        gateway._client_factory = lambda: _set_framing(fake, gateway)  # noqa: SLF001
        client = MbioClient(gateway, slave_id=3)

        with pytest.raises(MbioConnectionError) as raised:
            await client.async_probe()
        assert "slave id" in str(raised.value)
        assert "tcp/0x03" in str(raised.value)
        assert "rtu/0x03" in str(raised.value)

    async def test_a_rejecting_board_is_a_different_message(self) -> None:
        """An exception response proves the framing is right."""
        client = MbioClient(
            _gateway(FakeClient(read_error_response=True), retries=1), slave_id=3
        )
        with pytest.raises(
            MbioConnectionError, match="rejected every read function code"
        ):
            await client.async_probe()
