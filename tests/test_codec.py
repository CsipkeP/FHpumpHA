"""Codec tests.

The temp/O and dtime vectors come straight from the manual, via DESIGN.md
sections 2 and 3.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fujitsu_waterstage.codec import (
    CodecError,
    RegisterType,
    decode,
    decode_dtime,
    decode_temp,
    decode_uint16,
    decode_uint32,
    encode,
    encode_dtime,
    encode_temp,
    encode_uint16,
    encode_uint32,
    to_int16,
    uint32_to_words,
    words_to_uint32,
)


class TestInt16:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0x0000, 0),
            (0x0065, 101),
            (0x7FFF, 32767),
            (0x8000, -32768),
            (0xFF9B, -101),
            (0xFFFF, -1),
        ],
    )
    def test_to_int16(self, raw: int, expected: int) -> None:
        assert to_int16(raw) == expected

    def test_round_trip(self) -> None:
        for value in range(-32768, 32768, 97):
            assert to_int16(value & 0xFFFF) == value


class TestTemp:
    """DESIGN.md section 3 -- the four temp/O bit combinations, verbatim."""

    @pytest.mark.parametrize(
        ("raw", "celsius", "disabled"),
        [
            (0x0065, 10.1, False),
            (0x4065, 10.1, True),
            (0xBF9B, -10.1, True),
            (0xFF9B, -10.1, False),
        ],
    )
    def test_optional_bit_combinations(
        self, raw: int, celsius: float, disabled: bool
    ) -> None:
        assert decode_temp(raw, optional=True) == (celsius, disabled)

    @pytest.mark.parametrize(
        ("raw", "celsius"),
        [
            (0x0065, 10.1),
            (0xFF9B, -10.1),
            (0x0000, 0.0),
            (0x0226, 55.0),
            (0xFE0C, -50.0),
        ],
    )
    def test_plain_temp_is_a_signed_tenth_of_a_degree(
        self, raw: int, celsius: float
    ) -> None:
        assert decode_temp(raw) == (celsius, False)

    def test_plain_temp_never_reports_disabled(self) -> None:
        # Without /O, bit 14 is just part of the number.
        assert decode_temp(0x4065) == (1648.5, False)

    def test_scaling_is_not_one_sixty_fourth(self) -> None:
        assert decode_temp(101).value == 10.1

    @pytest.mark.parametrize(
        ("celsius", "raw"),
        [(10.1, 0x0065), (-10.1, 0xFF9B), (0.0, 0x0000), (-50.0, 0xFE0C), (55.0, 0x0226)],
    )
    def test_encode(self, celsius: float, raw: int) -> None:
        assert encode_temp(celsius) == (raw,)

    def test_encode_never_sets_the_disable_bit(self) -> None:
        """Writes always produce an enabled value (DESIGN.md section 10)."""
        for tenths in range(-1000, 1001):
            raw = encode_temp(tenths / 10, optional=True)[0]
            value, disabled = decode_temp(raw, optional=True)
            assert not disabled
            assert value == pytest.approx(tenths / 10)

    def test_encode_rejects_an_ambiguous_optional_value(self) -> None:
        with pytest.raises(CodecError):
            encode_temp(2000.0, optional=True)

    def test_encode_rejects_out_of_int16_range(self) -> None:
        with pytest.raises(CodecError):
            encode_temp(4000.0)

    def test_rounding_is_half_away_from_zero(self) -> None:
        assert encode_temp(20.25) == (203,)
        assert encode_temp(-20.25) == (0x10000 - 203,)


class TestUint16:
    def test_plain(self) -> None:
        assert decode_uint16(0xFFFF) == (65535, False)
        assert decode_uint16(0x8001) == (32769, False)

    def test_optional_uses_bit_15(self) -> None:
        assert decode_uint16(0x0064, optional=True) == (100, False)
        assert decode_uint16(0x8064, optional=True) == (100, True)
        assert decode_uint16(0xFFFF, optional=True) == (32767, True)

    def test_scale_one_tenth(self) -> None:
        """Register 23 -- compressor starts per hour run, scale 0.1."""
        assert decode_uint16(35, scale=0.1) == (3.5, False)
        assert decode_uint16(1, scale=0.1) == (0.1, False)
        assert decode_uint16(3, scale=0.1).value == 0.3  # no binary-float noise

    def test_scale_one_hundredth(self) -> None:
        """Register 105 -- heating curve slope, scale 0.01."""
        assert decode_uint16(250, scale=0.01) == (2.5, False)
        assert decode_uint16(10, scale=0.01) == (0.1, False)
        assert decode_uint16(400, scale=0.01) == (4.0, False)
        assert decode_uint16(7, scale=0.01).value == 0.07

    def test_integer_scale_keeps_an_integer(self) -> None:
        """Register 9905 -- baud rate, scale 10."""
        decoded = decode_uint16(960, scale=10)
        assert decoded == (9600, False)
        assert isinstance(decoded.value, int)

    @pytest.mark.parametrize(
        ("value", "scale", "raw"),
        [(3.5, 0.1, 35), (2.5, 0.01, 250), (0.1, 0.01, 10), (9600, 10, 960), (42, None, 42)],
    )
    def test_encode(self, value: float, scale: float | None, raw: int) -> None:
        assert encode_uint16(value, scale=scale) == (raw,)

    def test_encode_rejects_out_of_range(self) -> None:
        with pytest.raises(CodecError):
            encode_uint16(-1)
        with pytest.raises(CodecError):
            encode_uint16(65536)


class TestUint32:
    def test_high_word_first(self) -> None:
        assert words_to_uint32(0x1234, 0x5678) == 0x1234_5678
        assert uint32_to_words(0x1234_5678) == (0x1234, 0x5678)

    def test_decode(self) -> None:
        assert decode_uint32(0x0001, 0x0000) == (65536, False)
        assert decode_uint32(0x0000, 0x0001) == (1, False)
        assert decode_uint32(0xFFFF, 0xFFFF) == (0xFFFF_FFFF, False)

    def test_optional_uses_bit_31(self) -> None:
        assert decode_uint32(0x8000, 0x0001, optional=True) == (1, True)
        assert decode_uint32(0x0000, 0x0001, optional=True) == (1, False)

    def test_encode(self) -> None:
        assert encode_uint32(65536) == (0x0001, 0x0000)
        assert encode_uint32(0) == (0, 0)

    def test_encode_rejects_out_of_range(self) -> None:
        with pytest.raises(CodecError):
            encode_uint32(0x1_0000_0000)


class TestDtime:
    """DESIGN.md section 2 -- the three vectors from the manual, verbatim."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0x07B4_5AD4, datetime(2023, 4, 11, 11, 20)),
            (0x07B4_C359, datetime(2023, 4, 24, 13, 25)),
        ],
    )
    def test_manual_vectors(self, raw: int, expected: datetime) -> None:
        assert decode_dtime(*uint32_to_words(raw)) == (expected, False)

    def test_disabled_vector(self) -> None:
        value, disabled = decode_dtime(*uint32_to_words(0x8000_0000))
        assert disabled is True
        assert value is None  # month 0 / day 0 is not a date

    def test_bit_31_disables_a_valid_timestamp(self) -> None:
        value, disabled = decode_dtime(*uint32_to_words(0x8000_0000 | 0x07B4_5AD4))
        assert disabled is True
        assert value == datetime(2023, 4, 11, 11, 20)

    def test_empty_slot_decodes_to_none(self) -> None:
        assert decode_dtime(0, 0) == (None, False)

    def test_unused_bits_28_to_30_are_ignored(self) -> None:
        raw = 0x07B4_5AD4 | 0x7000_0000
        assert decode_dtime(*uint32_to_words(raw)) == (datetime(2023, 4, 11, 11, 20), False)

    @pytest.mark.parametrize(
        "moment",
        [
            datetime(2023, 4, 11, 11, 20),
            datetime(2023, 4, 24, 13, 25),
            datetime(1900, 1, 1, 0, 0),
            datetime(2155, 12, 31, 23, 59),
        ],
    )
    def test_round_trip(self, moment: datetime) -> None:
        assert decode_dtime(*encode_dtime(moment)) == (moment, False)

    def test_encode_disabled(self) -> None:
        words = encode_dtime(datetime(2023, 4, 11, 11, 20), disabled=True)
        assert decode_dtime(*words) == (datetime(2023, 4, 11, 11, 20), True)

    def test_encode_rejects_year_out_of_range(self) -> None:
        with pytest.raises(CodecError):
            encode_dtime(datetime(2200, 1, 1))


class TestDispatch:
    @pytest.mark.parametrize(
        ("register_type", "length"),
        [
            (RegisterType.UINT16, 1),
            (RegisterType.TEMP, 1),
            (RegisterType.UINT32, 2),
            (RegisterType.DTIME, 2),
        ],
    )
    def test_type_length(self, register_type: RegisterType, length: int) -> None:
        assert register_type.length == length

    def test_decode_dispatches(self) -> None:
        assert decode([0x0065], "temp") == (10.1, False)
        assert decode([0x4065], "temp", optional=True) == (10.1, True)
        assert decode([250], "uint16", scale=0.01) == (2.5, False)
        assert decode([0x0001, 0x0000], "uint32") == (65536, False)
        assert decode(uint32_to_words(0x07B4_5AD4), "dtime").value == datetime(
            2023, 4, 11, 11, 20
        )

    @pytest.mark.parametrize(
        ("words", "register_type"),
        [([1, 2], "temp"), ([1], "uint32"), ([1], "dtime"), ([], "uint16")],
    )
    def test_decode_rejects_a_wrong_word_count(
        self, words: list[int], register_type: str
    ) -> None:
        with pytest.raises(CodecError):
            decode(words, register_type)

    def test_encode_dispatches(self) -> None:
        assert encode(10.1, "temp") == (0x0065,)
        assert encode(2.5, "uint16", scale=0.01) == (250,)
        assert encode(65536, "uint32") == (1, 0)
        assert encode(datetime(2023, 4, 11, 11, 20), "dtime") == uint32_to_words(
            0x07B4_5AD4
        )

    def test_encode_dtime_needs_a_datetime(self) -> None:
        with pytest.raises(CodecError):
            encode(5, "dtime")

    def test_unknown_type(self) -> None:
        with pytest.raises(ValueError):
            decode([0], "float32")
