"""Data-type codec for the Fujitsu Waterstage FWS-MBIO-002 Modbus interface.

``mbio_registers.json`` uses exactly four register types:

``uint16``
    Unsigned 16 bit integer, optionally with a ``scale`` factor.
``temp``
    **Signed** int16 in 0.1 °C steps -- ``°C = int16(raw) / 10``.  Not 1/64.
``uint32``
    Two consecutive registers, **high word first**.
``dtime``
    Two consecutive registers holding a packed date/time.

Registers whose access contains ``/O`` (``R/O``, ``R/W/O``) may be flagged
*disabled* by the controller.  The flag sits in a different bit for every type,
and ``temp`` is the one that is easy to get wrong because bit 15 is also the
sign bit:

===========  ================================  ==============================
type         disabled when                     value recovered by
===========  ================================  ==============================
``uint16/O`` bit 15 set                        ``raw & 0x7FFF``
``uint32/O`` bit 31 set                        ``raw & 0x7FFF_FFFF``
``dtime``    bit 31 set (always, not just /O)  ``raw & 0x7FFF_FFFF``
``temp/O``   bit 15 XOR bit 14                 ``raw ^ 0x4000``, then int16
===========  ================================  ==============================

A disabled data point still decodes to a value, but the value is not valid --
the entity layer must publish it as ``unavailable``, never as ``0``.

See ``docs/DESIGN.md`` sections 2 and 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, NamedTuple

__all__ = [
    "CodecError",
    "DecodedValue",
    "RegisterType",
    "decode",
    "decode_dtime",
    "decode_temp",
    "decode_uint16",
    "decode_uint32",
    "encode",
    "encode_dtime",
    "encode_temp",
    "encode_uint16",
    "encode_uint32",
    "to_int16",
    "uint32_to_words",
    "words_to_uint32",
]

# ---------------------------------------------------------------------------
# Bit layout constants
# ---------------------------------------------------------------------------

UINT16_MASK: Final = 0xFFFF
UINT32_MASK: Final = 0xFFFF_FFFF

INT16_MIN: Final = -0x8000
INT16_MAX: Final = 0x7FFF

#: ``uint16/O``: bit 15 marks the data point as disabled.
UINT16_DISABLE_BIT: Final = 0x8000
UINT16_VALUE_MASK: Final = 0x7FFF

#: ``uint32/O`` and ``dtime``: bit 31 marks the data point as disabled.
UINT32_DISABLE_BIT: Final = 0x8000_0000
UINT32_VALUE_MASK: Final = 0x7FFF_FFFF

#: ``temp/O``: bit 14 relative to the sign bit -- see :func:`decode_temp`.
TEMP_DISABLE_BIT: Final = 0x4000

#: Temperatures are transported as int16 in tenths of a degree.
TEMP_SCALE: Final = 10

# ``dtime`` packing, DESIGN.md section 2.
DTIME_YEAR_SHIFT: Final = 20
DTIME_YEAR_MASK: Final = 0xFF
DTIME_YEAR_EPOCH: Final = 1900
DTIME_MONTH_SHIFT: Final = 16
DTIME_MONTH_MASK: Final = 0x0F
DTIME_DAY_SHIFT: Final = 11
DTIME_DAY_MASK: Final = 0x1F
DTIME_HOUR_SHIFT: Final = 6
DTIME_HOUR_MASK: Final = 0x1F
DTIME_MINUTE_MASK: Final = 0x3F


class CodecError(ValueError):
    """A value cannot be represented in the register's data type."""


class RegisterType(StrEnum):
    """The four data types used by the MBIO register map."""

    UINT16 = "uint16"
    TEMP = "temp"
    UINT32 = "uint32"
    DTIME = "dtime"

    @property
    def length(self) -> int:
        """Number of Modbus registers this type occupies."""
        return 2 if self in (RegisterType.UINT32, RegisterType.DTIME) else 1


class DecodedValue(NamedTuple):
    """A decoded data point plus its ``/O`` disabled flag.

    ``value`` stays readable even when ``disabled`` is set; it is simply not
    meaningful.  ``value`` is ``None`` only when the raw content cannot be
    interpreted at all (an unset ``dtime`` slot, for example).
    """

    value: int | float | datetime | None
    disabled: bool


# ---------------------------------------------------------------------------
# Word helpers
# ---------------------------------------------------------------------------


def to_int16(raw: int) -> int:
    """Reinterpret a 16 bit word as a signed two's complement integer."""
    raw &= UINT16_MASK
    return raw - 0x1_0000 if raw & UINT16_DISABLE_BIT else raw


def from_int16(value: int) -> int:
    """Encode a signed integer as a 16 bit two's complement word."""
    if not INT16_MIN <= value <= INT16_MAX:
        raise CodecError(f"{value} does not fit in an int16")
    return value & UINT16_MASK


def words_to_uint32(high: int, low: int) -> int:
    """Join two registers, high word first."""
    return ((high & UINT16_MASK) << 16) | (low & UINT16_MASK)


def uint32_to_words(value: int) -> tuple[int, int]:
    """Split a 32 bit value into two registers, high word first."""
    if not 0 <= value <= UINT32_MASK:
        raise CodecError(f"{value} does not fit in a uint32")
    return (value >> 16) & UINT16_MASK, value & UINT16_MASK


def _to_decimal(value: float | int | Decimal | str) -> Decimal:
    """Convert to Decimal via ``str`` so 0.1 stays 0.1 instead of 0.1000...055."""
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as err:  # pragma: no cover - defensive
        raise CodecError(f"{value!r} is not a number") from err


def _round_half_up(value: Decimal) -> int:
    """Round to the nearest integer, ties away from zero."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _apply_scale(raw: int, scale: int | float | None) -> int | float:
    """Turn a raw register value into the physical value.

    An integer ``scale`` (9905 baud rate: 10) keeps the result an ``int``; a
    fractional ``scale`` (0.1, 0.01) produces a ``float`` that is free of
    binary-float noise because the multiplication runs in :class:`Decimal`.
    """
    if scale is None:
        return raw
    if isinstance(scale, int):
        return raw * scale
    return float(_to_decimal(scale) * raw)


def _remove_scale(value: float | int, scale: int | float | None) -> int:
    """Turn a physical value back into a raw register value."""
    if scale is None:
        return _round_half_up(_to_decimal(value))
    return _round_half_up(_to_decimal(value) / _to_decimal(scale))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode_uint16(
    raw: int, *, optional: bool = False, scale: int | float | None = None
) -> DecodedValue:
    """Decode a ``uint16`` register.

    With ``optional`` (access ``R/O`` or ``R/W/O``) bit 15 flags the data point
    as disabled and the value lives in the remaining 15 bits.
    """
    raw &= UINT16_MASK
    disabled = False
    if optional:
        disabled = bool(raw & UINT16_DISABLE_BIT)
        raw &= UINT16_VALUE_MASK
    return DecodedValue(_apply_scale(raw, scale), disabled)


def decode_temp(raw: int, *, optional: bool = False) -> DecodedValue:
    """Decode a ``temp`` register into degrees Celsius.

    Plain ``temp`` is a straight int16 in tenths of a degree.  For ``temp/O``
    the disable flag is *relative to the sign bit*: the data point is disabled
    when bit 15 and bit 14 differ, and the true value is recovered by flipping
    bit 14 back.

    ======  ======  ========  ==================================
    bit 15  bit 14  disabled  example
    ======  ======  ========  ==================================
    0       0       no        ``0x0065`` ->  10.1 °C
    0       1       **yes**   ``0x4065`` ->  10.1 °C, disabled
    1       0       **yes**   ``0xBF9B`` -> -10.1 °C, disabled
    1       1       no        ``0xFF9B`` -> -10.1 °C
    ======  ======  ========  ==================================
    """
    raw &= UINT16_MASK
    if not optional:
        return DecodedValue(to_int16(raw) / TEMP_SCALE, False)
    disabled = bool(((raw >> 15) & 1) ^ ((raw >> 14) & 1))
    corrected = raw ^ TEMP_DISABLE_BIT if disabled else raw
    return DecodedValue(to_int16(corrected) / TEMP_SCALE, disabled)


def decode_uint32(
    high: int,
    low: int,
    *,
    optional: bool = False,
    scale: int | float | None = None,
) -> DecodedValue:
    """Decode a two-register ``uint32``, high word first."""
    raw = words_to_uint32(high, low)
    disabled = False
    if optional:
        disabled = bool(raw & UINT32_DISABLE_BIT)
        raw &= UINT32_VALUE_MASK
    return DecodedValue(_apply_scale(raw, scale), disabled)


def decode_dtime(high: int, low: int) -> DecodedValue:
    """Decode a two-register packed date/time.

    Bit 31 marks the timestamp as disabled -- for ``dtime`` this is part of the
    type itself, not of the ``/O`` access flag.  Slots that hold no date at all
    (an empty fault history entry) decode to ``None``.
    """
    raw = words_to_uint32(high, low)
    disabled = bool(raw & UINT32_DISABLE_BIT)
    packed = raw & UINT32_VALUE_MASK

    year = ((packed >> DTIME_YEAR_SHIFT) & DTIME_YEAR_MASK) + DTIME_YEAR_EPOCH
    month = (packed >> DTIME_MONTH_SHIFT) & DTIME_MONTH_MASK
    day = (packed >> DTIME_DAY_SHIFT) & DTIME_DAY_MASK
    hour = (packed >> DTIME_HOUR_SHIFT) & DTIME_HOUR_MASK
    minute = packed & DTIME_MINUTE_MASK

    try:
        value: datetime | None = datetime(year, month, day, hour, minute)
    except ValueError:
        # Unset or nonsensical slot (month/day zero, hour 31, ...).
        value = None
    return DecodedValue(value, disabled)


def decode(
    words: Sequence[int],
    register_type: RegisterType | str,
    *,
    optional: bool = False,
    scale: int | float | None = None,
) -> DecodedValue:
    """Decode ``words`` according to ``register_type``.

    ``words`` must hold exactly as many registers as the type occupies.
    """
    register_type = RegisterType(register_type)
    if len(words) != register_type.length:
        raise CodecError(
            f"{register_type} needs {register_type.length} register(s), "
            f"got {len(words)}"
        )
    match register_type:
        case RegisterType.UINT16:
            return decode_uint16(words[0], optional=optional, scale=scale)
        case RegisterType.TEMP:
            return decode_temp(words[0], optional=optional)
        case RegisterType.UINT32:
            return decode_uint32(words[0], words[1], optional=optional, scale=scale)
        case RegisterType.DTIME:
            return decode_dtime(words[0], words[1])
    raise CodecError(f"unsupported register type {register_type}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Encoding
#
# Writes never set the disable bit: a parameter is enabled or disabled from the
# controller's own menu, never over Modbus (DESIGN.md section 10).
# ---------------------------------------------------------------------------


def encode_uint16(
    value: float | int, *, scale: int | float | None = None
) -> tuple[int]:
    """Encode a physical value into a single ``uint16`` register."""
    raw = _remove_scale(value, scale)
    if not 0 <= raw <= UINT16_MASK:
        raise CodecError(f"{value} is out of range for a uint16 register")
    return (raw,)


def encode_temp(value: float | int, *, optional: bool = False) -> tuple[int]:
    """Encode degrees Celsius into a single ``temp`` register.

    Two's complement already yields ``bit 15 == bit 14`` for every temperature
    in the ±1638.3 °C window, so a normal write never looks disabled.  With
    ``optional`` set, values outside that window are rejected rather than
    silently written as a disabled data point.
    """
    raw_signed = _round_half_up(_to_decimal(value) * TEMP_SCALE)
    raw = from_int16(raw_signed)
    if optional and (((raw >> 15) & 1) ^ ((raw >> 14) & 1)):
        raise CodecError(
            f"{value} °C cannot be written to a temp/O register: the encoding "
            "would read back as disabled"
        )
    return (raw,)


def encode_uint32(
    value: float | int, *, scale: int | float | None = None
) -> tuple[int, int]:
    """Encode a physical value into two registers, high word first."""
    raw = _remove_scale(value, scale)
    if not 0 <= raw <= UINT32_MASK:
        raise CodecError(f"{value} is out of range for a uint32 register")
    return uint32_to_words(raw)


def encode_dtime(value: datetime, *, disabled: bool = False) -> tuple[int, int]:
    """Encode a date/time into two registers, high word first."""
    year = value.year - DTIME_YEAR_EPOCH
    if not 0 <= year <= DTIME_YEAR_MASK:
        raise CodecError(f"{value.year} is outside the dtime year range")
    packed = (
        (year << DTIME_YEAR_SHIFT)
        | (value.month << DTIME_MONTH_SHIFT)
        | (value.day << DTIME_DAY_SHIFT)
        | (value.hour << DTIME_HOUR_SHIFT)
        | value.minute
    )
    if disabled:
        packed |= UINT32_DISABLE_BIT
    return uint32_to_words(packed)


def encode(
    value: float | int | datetime,
    register_type: RegisterType | str,
    *,
    optional: bool = False,
    scale: int | float | None = None,
) -> tuple[int, ...]:
    """Encode ``value`` according to ``register_type``."""
    register_type = RegisterType(register_type)
    match register_type:
        case RegisterType.UINT16:
            return encode_uint16(value, scale=scale)  # type: ignore[arg-type]
        case RegisterType.TEMP:
            return encode_temp(value, optional=optional)  # type: ignore[arg-type]
        case RegisterType.UINT32:
            return encode_uint32(value, scale=scale)  # type: ignore[arg-type]
        case RegisterType.DTIME:
            if not isinstance(value, datetime):
                raise CodecError("a dtime register needs a datetime value")
            return encode_dtime(value)
    raise CodecError(f"unsupported register type {register_type}")  # pragma: no cover
