#!/usr/bin/env python3
"""Dump the whole FWS-MBIO-002 register map from a real board.

Standalone: needs ``pymodbus``, but no Home Assistant.  This is the tool that
verifies the register map against the hardware before any entity code is built
on top of it (DESIGN.md section 14, phase 2).

    python tools/dump.py --host 192.168.1.37 --port 502 --slave 3

The board needs roughly four minutes after power-up to refresh every parameter
over the BSB bus, and a read that arrives earlier may answer 0 while also
triggering the BSB query.  That is why two rounds are read by default and the
second one is the one printed (DESIGN.md section 5).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "fujitsu_waterstage"


def _import_component() -> types.ModuleType:
    """Import the integration modules without running its Home Assistant setup.

    A stand-in package object with ``__path__`` lets ``fujitsu_waterstage.hub``
    resolve its relative imports while ``__init__.py`` -- which pulls in Home
    Assistant -- is never executed.
    """
    if "fujitsu_waterstage" not in sys.modules:
        package = types.ModuleType("fujitsu_waterstage")
        package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules["fujitsu_waterstage"] = package
    import importlib

    return importlib.import_module("fujitsu_waterstage.hub")


hub = _import_component()
import fujitsu_waterstage.registers as registers_module  # noqa: E402

Register = registers_module.Register
load_register_map = registers_module.load_register_map


LINK_STATUS_ADDRESS = 0
PRODUCT_CODE_ADDRESS = 9900


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="RS-485/TCP gateway address")
    parser.add_argument("--port", type=int, default=hub.DEFAULT_PORT)
    parser.add_argument(
        "--slave", type=int, default=1, help="Modbus slave id of the MBIO board"
    )
    parser.add_argument(
        "--function-code",
        type=lambda value: int(value, 0),
        choices=(hub.FUNCTION_READ_HOLDING, hub.FUNCTION_READ_INPUT),
        default=None,
        help="3 = read holding, 4 = read input; probed automatically by default",
    )
    parser.add_argument("--timeout", type=float, default=hub.DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=hub.DEFAULT_RETRIES)
    parser.add_argument(
        "--delay",
        type=float,
        default=hub.DEFAULT_INTER_REQUEST_DELAY,
        help="seconds of bus idle time between requests",
    )
    parser.add_argument(
        "--max-registers",
        type=int,
        default=hub.MAX_REGISTERS_PER_READ,
        help="largest read request (Modbus RTU allows at most 125)",
    )
    parser.add_argument(
        "--max-gap",
        type=int,
        default=hub.DEFAULT_MAX_GAP,
        help="unused addresses that may be merged into one request",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=2,
        help="read passes; only the last one is printed (default 2, see DESIGN.md 5)",
    )
    parser.add_argument(
        "--round-delay", type=float, default=10.0, help="seconds between read passes"
    )
    parser.add_argument(
        "--block",
        action="append",
        dest="blocks",
        metavar="NAME",
        help="limit to a functional block; repeatable",
    )
    parser.add_argument(
        "--format", choices=("table", "json", "csv"), default="table"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write to a file instead of stdout"
    )
    parser.add_argument(
        "--groups-only",
        action="store_true",
        help="print the read groups that would be used and exit, without connecting",
    )
    return parser.parse_args(argv)


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    # str() on a float keeps the trailing ".0", so a scaled 25.0 °C is visibly
    # different from a raw count of 25.  Decimal scaling keeps it noise-free.
    return str(value)


def _format_raw(words: tuple[int, ...] | None) -> str:
    if words is None:
        return "-"
    return " ".join(f"{word:04X}" for word in words)


class Row(dict[str, Any]):
    """One dumped register."""


def _rows(
    register_map: registers_module.RegisterMap,
    selected: list[Register],
    raw_words: dict[str, tuple[int, ...]],
    errors: dict[str, str],
) -> list[Row]:
    rows: list[Row] = []
    for register in selected:
        words = raw_words.get(register.key)
        error = errors.get(register.key)
        value: Any = None
        disabled = False
        meaning: str | None = None
        if words is not None:
            decoded = register.decode(words)
            value, disabled = decoded.value, decoded.disabled
            if register.options is not None or register.options_ref is not None:
                raw = words[0] if register.length == 1 else None
                if raw is not None:
                    meaning = register_map.describe(register, raw)
        rows.append(
            Row(
                address=register.address,
                key=register.key,
                name=register.name,
                block=register.block,
                type=str(register.type),
                access=register.access,
                unit=register.unit,
                raw=list(words) if words is not None else None,
                value=(
                    value.isoformat(timespec="minutes")
                    if isinstance(value, datetime)
                    else value
                ),
                disabled=disabled,
                meaning=meaning,
                error=error,
            )
        )
    return rows


def _render_table(rows: list[Row]) -> str:
    header = ("Addr", "Raw", "Value", "Unit", "Dis", "Acc", "Register", "Meaning")
    body: list[tuple[str, ...]] = []
    for row in rows:
        if row["error"]:
            body.append(
                (
                    str(row["address"]),
                    "ERR",
                    row["error"][:40],
                    "",
                    "",
                    row["access"],
                    f"{row['block']} / {row['name']}",
                    "",
                )
            )
            continue
        raw = tuple(row["raw"]) if row["raw"] is not None else None
        value = row["value"]
        body.append(
            (
                str(row["address"]),
                _format_raw(raw),
                _format_value(value),
                row["unit"] or "",
                "yes" if row["disabled"] else "",
                row["access"],
                f"{row['block']} / {row['name']}",
                row["meaning"] or "",
            )
        )

    widths = [
        max(len(header[i]), max((len(line[i]) for line in body), default=0))
        for i in range(len(header))
    ]
    lines = [
        "  ".join(text.ljust(widths[i]) for i, text in enumerate(header)).rstrip(),
        "  ".join("-" * width for width in widths).rstrip(),
    ]
    lines += [
        "  ".join(text.ljust(widths[i]) for i, text in enumerate(line)).rstrip()
        for line in body
    ]
    return "\n".join(lines)


def _render_csv(rows: list[Row]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    fields = [
        "address",
        "key",
        "block",
        "name",
        "type",
        "access",
        "unit",
        "raw",
        "value",
        "disabled",
        "meaning",
        "error",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        record = dict(row)
        record["raw"] = _format_raw(tuple(row["raw"]) if row["raw"] is not None else None)
        writer.writerow(record)
    return buffer.getvalue()


async def _read_all(
    client: Any, groups: tuple[Any, ...]
) -> tuple[dict[str, tuple[int, ...]], dict[str, str]]:
    raw_words: dict[str, tuple[int, ...]] = {}
    errors: dict[str, str] = {}
    for group in groups:
        try:
            words = await client.async_read_group(group)
        except hub.MbioError as err:
            print(
                f"! group {group.start}..{group.end} failed: {err}",
                file=sys.stderr,
            )
            for register in group.registers:
                errors[register.key] = str(err)
            continue
        for register in group.registers:
            raw_words[register.key] = group.words_for(register, words)
    return raw_words, errors


async def _run(args: argparse.Namespace) -> int:
    register_map = load_register_map()
    selected = list(
        register_map.in_blocks(args.blocks) if args.blocks else register_map.registers
    )
    if not selected:
        print(
            f"No registers in blocks {args.blocks}; known blocks: "
            f"{', '.join(register_map.blocks)}",
            file=sys.stderr,
        )
        return 2

    groups = hub.build_read_groups(
        selected, max_registers=args.max_registers, max_gap=args.max_gap
    )
    print(
        f"{len(selected)} registers in {len(groups)} read request(s): "
        + ", ".join(f"{group.start}..{group.end}" for group in groups),
        file=sys.stderr,
    )
    if args.groups_only:
        return 0

    gateway = hub.ModbusGateway(
        args.host,
        args.port,
        timeout=args.timeout,
        retries=args.retries,
        inter_request_delay=args.delay,
    )
    client = hub.MbioClient(gateway, args.slave)

    try:
        if args.function_code is None:
            code = await client.async_probe_function_code()
            print(f"Reading with function code {code:#04x}", file=sys.stderr)
        else:
            client.function_code = args.function_code

        raw_words: dict[str, tuple[int, ...]] = {}
        errors: dict[str, str] = {}
        for round_number in range(1, max(1, args.rounds) + 1):
            if round_number > 1:
                print(
                    f"Waiting {args.round_delay:g}s before pass {round_number}...",
                    file=sys.stderr,
                )
                await asyncio.sleep(args.round_delay)
            raw_words, errors = await _read_all(client, groups)
            print(
                f"Pass {round_number}: {len(raw_words)} registers read, "
                f"{len(errors)} failed",
                file=sys.stderr,
            )
    except hub.MbioError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        await gateway.async_close()

    link = raw_words.get("interface_communication_status")
    if link is not None:
        print(
            "BSB link: "
            + ("OK" if link[0] else "ERROR - the board cannot reach the RVS21"),
            file=sys.stderr,
        )
    product = raw_words.get("interface_product_code")
    if product is not None and product[0] != hub.MBIO_PRODUCT_CODE:
        print(
            f"! register 9900 is {product[0]:#06x}, not {hub.MBIO_PRODUCT_CODE:#06x} - "
            "this may not be an MBIO board",
            file=sys.stderr,
        )

    rows = _rows(register_map, selected, raw_words, errors)
    match args.format:
        case "json":
            text = json.dumps(rows, indent=2, ensure_ascii=False)
        case "csv":
            text = _render_csv(rows)
        case _:
            text = _render_table(rows)

    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
