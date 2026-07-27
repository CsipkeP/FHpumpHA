"""Load ``mbio_registers.json`` into frozen dataclasses.

The JSON file shipped inside the package is the authoritative input for the
whole integration -- see ``docs/DESIGN.md``.  It is copied into the package (not
read from ``docs/``) so a HACS install works on its own.

Every register gets a :attr:`Register.key`, a stable snake_case identifier
derived from ``block`` + ``name``.  It is the second half of the entity
``unique_id`` (``f"{entry.entry_id}_{register.key}"``) and therefore **must not
change between releases**.  ``tests/register_keys.json`` pins all 204 of them.

Loading touches the filesystem, so Home Assistant must call
:func:`load_register_map` from an executor, not from the event loop.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .codec import DecodedValue, RegisterType, decode, encode

__all__ = [
    "ACCESS_RESET",
    "ACCESS_WRITE",
    "REGISTER_MAP_FILE",
    "Register",
    "RegisterMap",
    "load_register_map",
    "register_key",
]

#: The packaged copy of the register map.
REGISTER_MAP_FILE: Final = Path(__file__).with_name("mbio_registers.json")

#: Access-string components.  ``R/W/O`` -> {"R", "W", "O"}.
ACCESS_READ: Final = "R"
ACCESS_WRITE: Final = "W"
ACCESS_OPTIONAL: Final = "O"
ACCESS_RESET: Final = "Reset"

#: ``safety`` value that gates a register behind the ``expert`` write level.
SAFETY_EXPERT: Final = "expert"

#: ``role`` of register 0 -- the BSB link status (DESIGN.md section 4).
ROLE_LINK_STATUS: Final = "link_status"

_NON_KEY_CHARS: Final = re.compile(r"[^0-9A-Za-z]+")

#: Sanity bound when expanding a ``"64-127"`` style code-table key.
_MAX_CODE_RANGE: Final = 4096


def register_key(block: str, name: str) -> str:
    """Build the stable snake_case key for a register.

    Deterministic and dependent only on the two JSON fields: accents are folded
    away (``é`` -> ``e``), every run of non-alphanumeric characters -- including
    ``°`` -- collapses into a single underscore, and the result is lower-cased.

    >>> register_key("heating_circuit_1", "Heating curve 1 slope")
    'heating_circuit_1_heating_curve_1_slope'
    >>> register_key("cooling_circuit_1", "Flow temperature at 25 °C outdoor temp CC1")
    'cooling_circuit_1_flow_temperature_at_25_c_outdoor_temp_cc1'
    """
    folded = unicodedata.normalize("NFKD", f"{block} {name}")
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return _NON_KEY_CHARS.sub("_", folded).strip("_").lower()


@dataclass(frozen=True, slots=True)
class Register:
    """One entry of the MBIO register map."""

    key: str
    address: int
    type: RegisterType
    access: str
    block: str
    name: str
    length: int
    unit: str | None = None
    scale: int | float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    refresh_s: int | None = None
    rvs_param: int | None = None
    options: Mapping[int, str] | None = field(default=None, compare=False, repr=False)
    options_ref: str | None = None
    note: str | None = None
    safety: str | None = None
    role: str | None = None

    # -- access flags -----------------------------------------------------

    @property
    def _access_parts(self) -> tuple[str, ...]:
        return tuple(self.access.split("/"))

    @property
    def writable(self) -> bool:
        """``R/W`` or ``R/W/O`` -- the register accepts a value.

        Whether a *writable entity* is created is a separate decision driven by
        the ``write_level`` option (DESIGN.md section 10.1).
        """
        return ACCESS_WRITE in self._access_parts

    @property
    def resettable(self) -> bool:
        """``R/Reset`` -- writing any value clears the counter."""
        return ACCESS_RESET in self._access_parts

    @property
    def optional(self) -> bool:
        """``R/O`` or ``R/W/O`` -- the value carries a disable flag."""
        return ACCESS_OPTIONAL in self._access_parts

    @property
    def expert_only(self) -> bool:
        """Register that switches physical outputs or restarts hardware."""
        return self.safety == SAFETY_EXPERT

    @property
    def is_link_status(self) -> bool:
        """Register 0, the MBIO <-> RVS21 BSB link status."""
        return self.role == ROLE_LINK_STATUS

    # -- address span -----------------------------------------------------

    @property
    def end_address(self) -> int:
        """Address of the last register occupied by this data point."""
        return self.address + self.length - 1

    @property
    def addresses(self) -> range:
        """All Modbus addresses occupied by this data point."""
        return range(self.address, self.address + self.length)

    # -- conversion -------------------------------------------------------

    def decode(self, words: Sequence[int]) -> DecodedValue:
        """Decode the raw registers of exactly this data point."""
        return decode(words, self.type, optional=self.optional, scale=self.scale)

    def encode(self, value: Any) -> tuple[int, ...]:
        """Encode a physical value into raw registers (high word first)."""
        return encode(value, self.type, optional=self.optional, scale=self.scale)

    def validate(self, value: Any, *, check_step: bool = True) -> None:
        """Check a value against the JSON ``min`` / ``max`` / ``step``.

        Raises :class:`ValueError`; the service layer turns that into a
        ``ServiceValidationError``.
        """
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.key}: {value} is below the minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.key}: {value} is above the maximum {self.maximum}")
        if self.options is not None and value not in self.options:
            raise ValueError(
                f"{self.key}: {value} is not one of {sorted(self.options)}"
            )
        if check_step and self.step:
            base = self.minimum if self.minimum is not None else 0
            offset = round((value - base) / self.step)
            if abs(base + offset * self.step - value) > 1e-6:
                raise ValueError(
                    f"{self.key}: {value} is not a multiple of the step {self.step}"
                )

    def option_text(self, raw: int) -> str | None:
        """Resolve an inline ``options`` code.  ``options_ref`` needs the map."""
        if self.options is None:
            return None
        return self.options.get(raw)


@dataclass(frozen=True, slots=True)
class RegisterMap:
    """The complete register map plus the status/error code tables."""

    registers: tuple[Register, ...]
    board: Mapping[str, Any]
    status_codes: Mapping[int, str]
    error_codes: Mapping[int, str]
    mbio_error_codes: Mapping[int, str]
    source_document: str
    _by_key: Mapping[str, Register] = field(repr=False, compare=False)
    _by_address: Mapping[int, Register] = field(repr=False, compare=False)

    def __iter__(self) -> Iterator[Register]:
        return iter(self.registers)

    def __len__(self) -> int:
        return len(self.registers)

    def __getitem__(self, key: str) -> Register:
        return self._by_key[key]

    def get(self, key: str) -> Register | None:
        """Look a register up by its stable key."""
        return self._by_key.get(key)

    def at(self, address: int) -> Register | None:
        """Look a register up by its *start* address."""
        return self._by_address.get(address)

    @property
    def keys(self) -> tuple[str, ...]:
        """All register keys, in register-map order."""
        return tuple(register.key for register in self.registers)

    @property
    def addresses(self) -> frozenset[int]:
        """Every Modbus address the board implements.

        A read that touches anything outside this set is answered with an
        illegal-data-address exception, and the *whole* request is lost -- so
        this is what bounds a read group (DESIGN.md section 8.2).
        """
        return frozenset(
            address for register in self.registers for address in register.addresses
        )

    @property
    def blocks(self) -> tuple[str, ...]:
        """Functional block names, in first-appearance order."""
        return tuple(dict.fromkeys(register.block for register in self.registers))

    def in_blocks(self, blocks: Iterable[str]) -> tuple[Register, ...]:
        """Every register belonging to one of ``blocks``."""
        wanted = set(blocks)
        return tuple(r for r in self.registers if r.block in wanted)

    def code_table(self, name: str) -> Mapping[int, str]:
        """One of the ``options_ref`` tables by name."""
        tables = {
            "status_codes": self.status_codes,
            "error_codes": self.error_codes,
            "mbio_error_codes": self.mbio_error_codes,
        }
        try:
            return tables[name]
        except KeyError:
            raise KeyError(f"unknown code table {name!r}") from None

    def describe(self, register: Register, raw: int) -> str | None:
        """Human-readable text for a coded value, from ``options``/``options_ref``."""
        if register.options is not None:
            return register.options.get(raw)
        if register.options_ref is not None:
            return self.code_table(register.options_ref).get(raw)
        return None


def _int_keyed(table: Mapping[str, str]) -> Mapping[int, str]:
    """Turn a JSON code table into an ``int`` keyed mapping.

    Keys are decimal codes, or inclusive ranges such as ``"64-127"`` (the MBIO
    error table lumps a whole driver-error band under one description).  Ranges
    are expanded so every consumer can just look a code up.
    """
    expanded: dict[int, str] = {}
    for code, text in table.items():
        start, separator, end = code.partition("-")
        first = int(start)
        last = int(end) if separator else first
        if last < first or last - first > _MAX_CODE_RANGE:
            raise ValueError(f"implausible code range {code!r}")
        for value in range(first, last + 1):
            expanded[value] = text
    return MappingProxyType(expanded)


def _build_register(entry: Mapping[str, Any]) -> Register:
    register_type = RegisterType(entry["type"])
    length = entry["length"]
    if length != register_type.length:
        raise ValueError(
            f"register {entry['register']}: length {length} contradicts "
            f"type {register_type} ({register_type.length} register(s))"
        )
    options = entry.get("options")
    return Register(
        key=register_key(entry["block"], entry["name"]),
        address=entry["register"],
        type=register_type,
        access=entry["access"],
        block=entry["block"],
        name=entry["name"],
        length=length,
        unit=entry.get("unit"),
        scale=entry.get("scale"),
        minimum=entry.get("min"),
        maximum=entry.get("max"),
        step=entry.get("step"),
        refresh_s=entry.get("refresh_s"),
        rvs_param=entry.get("rvs_param"),
        options=_int_keyed(options) if options is not None else None,
        options_ref=entry.get("options_ref"),
        note=entry.get("note"),
        safety=entry.get("safety"),
        role=entry.get("role"),
    )


def _load(path: Path) -> RegisterMap:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    registers = tuple(_build_register(entry) for entry in raw["registers"])

    by_key: dict[str, Register] = {}
    by_address: dict[int, Register] = {}
    occupied: dict[int, Register] = {}
    for register in registers:
        if register.key in by_key:
            raise ValueError(
                f"duplicate register key {register.key!r} "
                f"({by_key[register.key].address} and {register.address})"
            )
        by_key[register.key] = register
        by_address[register.address] = register
        for address in register.addresses:
            if address in occupied:
                raise ValueError(
                    f"register {register.address} ({register.key}) overlaps "
                    f"{occupied[address].address} ({occupied[address].key})"
                )
            occupied[address] = register

    return RegisterMap(
        registers=registers,
        board=MappingProxyType(raw["board"]),
        status_codes=_int_keyed(raw["status_codes"]),
        error_codes=_int_keyed(raw["error_codes"]),
        mbio_error_codes=_int_keyed(raw["mbio_error_codes"]),
        source_document=raw["source_document"],
        _by_key=MappingProxyType(by_key),
        _by_address=MappingProxyType(by_address),
    )


@cache
def load_register_map(path: Path | str | None = None) -> RegisterMap:
    """Load and cache the register map.

    Blocking file I/O -- call it from an executor inside Home Assistant.
    """
    return _load(Path(path) if path is not None else REGISTER_MAP_FILE)
