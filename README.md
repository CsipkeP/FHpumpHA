# Fujitsu Waterstage (FWS-MBIO Modbus)

A Home Assistant integration for a Fujitsu Waterstage air-to-water heat pump,
read through a **Fujitsu Waterstage FWS-MBIO-002** Modbus interface board.

> **This is not an official integration.** It is not affiliated with, endorsed
> by, or supported by Fujitsu, ACITECH Solutions Kft. (the maker of the
> FWS-MBIO-002), or Columbus Klíma. "Fujitsu" and "Waterstage" are registered
> trademarks of their respective owners. Use it at your own risk: it writes to a
> heating controller, and a heating controller runs a machine that can freeze
> pipes or scald water if it is told the wrong thing.

Developed against an indoor `WSYK160DG9` / outdoor `WOYK112LCTA` pair with a
Siemens RVS21 controller. Other Waterstage models that use the same RVS21
controller and the same interface board should work, but have not been tried.

---

## What it does

The FWS-MBIO-002 sits on the heat pump's BSB bus, talks to the RVS21 controller,
and republishes what it learns as a Modbus RTU register map of 204 data points.
This integration reads that map over an RS-485 to TCP gateway and turns it into
Home Assistant entities.

- **Read**: flow, return and outside temperatures, compressor modulation, DHW
  tank temperature, circuit status, runtime counters, the fault history with
  timestamps, and the interface board's own diagnostics.
- **Write**: by default only the seven everyday setpoints. See
  [Write levels](#write-levels) — this is the part worth reading before you
  touch anything.
- **Two devices** in the registry: the heat pump, and the interface board. A
  problem with the board is visibly the board's.

### What it deliberately does not do

- **Time programs.** The interface board does not expose the weekly schedules
  at all. There is no register for them; this cannot be worked around.
- **Energy or COP.** There is no energy data in the register map. If you want
  it, meter the electricity separately and use Home Assistant's `integration`
  and `utility_meter` helpers.
- **Switching relay outputs or reading digital inputs.** These are wired and set
  with DIP switches on the board. Modbus can read a relay's *state* (450-459)
  but cannot drive it.
- **Configuring the interface board.** Slave address, baud rate and framing are
  DIP switches; the registers (9904-9906) are read-only.

---

## Hardware

```
Fujitsu Waterstage  --BSB-->  FWS-MBIO-002  --RS-485-->  Gateway  --TCP-->  Home Assistant
   (RVS21, X86)                (Modbus RTU slave)      (RS-485/TCP)
```

The interface board connects to the RVS21's BSB terminal and presents an RS-485
Modbus RTU slave. Either kind of RS-485 to TCP gateway works: a *protocol converting* one that
speaks Modbus TCP on the network side (what Home Assistant's own `modbus:`
platform calls `type: tcp`), or a *transparent* one that forwards raw RTU frames
(`type: rtuovertcp`). Setup probes both and remembers which one answered, so you
do not have to know. Wire the RS-485 A/B pair from the board to the gateway.

Getting this wrong is worth recognising: with the wrong framing **no slave
answers at all, with any function code**, which looks exactly like a dead bus.

**Follow the FWS-MBIO-002 manual for the actual terminals, polarity and power.**
This README does not repeat them, because getting them wrong on a live heating
controller is not a mistake worth risking on a second-hand description.

### DIP switches

| Switch | Meaning |
|---|---|
| `SW2` [1–4] | Modbus slave address, 1–15, in binary. **All off** means address 1 with a fixed 9600 8N1, ignoring the rest of `SW2`. |
| `SW2` [5–8] | Baud rate (9600 / 19200 / 28800 / 38400) and framing (8N1 / 8N2 / 8O1 / 8E1). See the manual for the combinations. |
| `ST1` | 120 Ω bus termination. On only at the two physical ends of the RS-485 bus. |

Up to 15 interface boards can share one bus. Other Modbus devices may share the
gateway too — the integration keeps a single connection per gateway address and
leaves an idle gap between requests so they get a turn.

### Optional

An **FWS-RB-002** relay module adds the QX31–QX35 relay outputs. Without it,
those registers simply read 0.

---

## Installation

### HACS (custom repository)

This repository is **not** in the HACS default list and is not intended to be —
the FWS-MBIO-002 is a niche product, and most Waterstage owners are better
served by [BSB-LAN](https://github.com/fredlcore/BSB-LAN). It is HACS-compatible
so it can be shared as a custom repository:

1. HACS → three-dot menu → **Custom repositories**
2. Repository: `https://github.com/CsipkeP/FHpumpHA`, category **Integration**
3. Install **Fujitsu Waterstage (FWS-MBIO Modbus)**, restart Home Assistant

### Manual

Copy `custom_components/fujitsu_waterstage/` into your Home Assistant
`config/custom_components/` directory and restart.

---

## Configuration

**Settings → Devices & services → Add integration → Fujitsu Waterstage.**

| Field | Notes |
|---|---|
| Gateway host | The RS-485/TCP gateway's address |
| Gateway port | 502 by default |
| Modbus slave address | Whatever `SW2` [1–4] is set to |
| Name | Shown as the device name |

Setup reads register **9900** and refuses anything that does not answer with the
MBIO product code `0x0401`. This matters: gateways are often shared, and writing
to a neighbouring device because a slave address was mistyped would be a real
problem. The framing (`tcp` / `rtu`) and both read function codes (`0x03` and
`0x04`) are probed, and the combination that works is remembered.

Setup then reads the whole register map twice, ten seconds apart, to work out
which parts of the installation exist. That is why adding the integration takes
about twenty seconds. Everything it decides can be corrected afterwards in the
options.

### Options

**Settings → Devices & services → Fujitsu Waterstage → Configure.**

| Option | Default | Notes |
|---|---|---|
| Function blocks | discovered | Turn on a block that exists but was not detected — see [Blocks](#blocks) |
| Room sensors | discovered | Which heating circuits have a room sensor — see [Thermostats](#thermostats) |
| Write level | `basic` | See [Write levels](#write-levels) |
| Fast / normal / slow interval | 30 / 120 / 300 s | Lower bounds are enforced, see [Polling](#polling) |
| Delay between requests | 50 ms | Idle time left on the RS-485 bus for other devices |
| Timeout, retries | 5 s, 3 | A single timeout is usually bus contention, not a fault |
| Registers per request | 120 | Below the Modbus RTU limit of 125 |

Changing any option reloads the integration.

---

## Write levels

`R/W` in the register map is a statement about the hardware, not a reason to put
a control on your dashboard. The write level decides what actually becomes
writable, and the default is deliberately narrow.

### `basic` (default)

Exactly seven registers are writable, covering everyday use and nothing else:

| Register | What | Entity |
|---|---|---|
| 40 | DHW operating mode | `water_heater` operation mode |
| 41 | DHW nominal setpoint | `water_heater` target temperature |
| 100 | HC1 operating mode | `climate` mode and preset |
| 101 | HC1 room comfort setpoint | `climate` target temperature |
| 102 | HC1 room reduced setpoint | `number` |
| 106 | Heating curve 1 parallel displacement | `number` |
| 107 | HC1 summer/winter changeover | `number` |

Every other `R/W` register — and there are more than fifty — appears as a
read-only sensor. You can see the heating curve slope; you cannot nudge it by
accident.

### `advanced`

Every `R/W` and `R/W/O` register becomes writable, categorised as configuration
so it stays off the main dashboard: setpoints, heating curves, legionella
settings, cooling parameters, HC2, CC1, CC2. Reset buttons appear for the
runtime and error counters — irreversible, but not dangerous.

### `expert`

Adds five registers that do something rather than hold a value:

| Register | What it does |
|---|---|
| 38 | Forces a defrost cycle |
| 39 | Restarts the heat pump |
| 460 | **Relay test — switches physical outputs**: pumps, valves, immersion heater |
| 461 | Overrides the UX2 analogue output |
| 9907 | Restarts the interface board (`0xAFAF`) |

Below `expert` these do not merely stay read-only: no entity is created for them
at all.

> **Lowering the level removes entities.** Home Assistant leaves them behind as
> "restored" until you delete them. This is Home Assistant's behaviour, not a
> bug in the integration.

### How a write works

1. The value is validated against the minimum, maximum and step in the register
   map. A bad value is refused before anything reaches the bus.
2. One register is written with function code `0x06`, two with `0x10`.
3. About two seconds later, only the read group containing that register is
   re-read. Until it lands, the entity shows what was written, so a slider does
   not snap back — and if the controller clamped the value, the read wins.
4. A rejected write raises an error and leaves the previous state in place.

For registers that can be disabled in the controller (`/O` access), the disable
bit is never set. Disabling a parameter is done from the controller's own menu.

---

## Entities

At the default write level, with every block enabled, this is about 197
entities: 165 sensors, 27 binary sensors, 3 numbers, 1 thermostat and 1 water
heater. Most installations have fewer, because discovery turns off the blocks
that are not fitted.

| Block | Registers | Enabled | Contents |
|---|---|---|---|
| Interface | 21 | always | Board identification, uptime, Modbus and BSB error counters, bus utilisation, BSB link status |
| Heat pump | 33 | always | Flow, return, outside and condenser temperatures, compressor modulation and status, immersion heaters, runtime and start counters, maintenance interval |
| Domestic hot water | 28 | always | Tank temperatures, pump and heater status, setpoints, legionella settings, runtime counters |
| Heating circuit 1 | 24 | always | Status, pump, mixing valve, room and flow temperatures, setpoints, heating curve |
| Faults | 22 | always | Active error count, ten-entry fault history with codes and timestamps, RVS software version |
| Relays | 12 | always | QX1–QX5 and QX31–QX35 output states |
| Heating circuit 2 | 24 | discovered | As HC1 |
| Cooling circuit 1 / 2 | 17 each | discovered | Status, flow temperatures, cooling limits, summer compensation |
| Solar, buffer, supplementary source | 1 each | discovered | Status code |
| Swimming pool | 3 | discovered | Temperature, setpoint, status |

Status and error codes are published as text, with the raw controller code kept
in a `code` attribute so automations and bug reports still have the number.

### Devices

Entities are split across two devices linked with `via_device`:

- **Fujitsu Waterstage** — everything the RVS21 controller produces.
- **Waterstage Modbus I/O Board** — the interface registers (9900–9921) and the
  heat exchanger temperature (13), which the board measures itself.

The split is not cosmetic. When the BSB link between board and controller fails,
the first device's entities go unavailable and the second device's stay valid —
which is exactly the information you need to tell "the heat pump is off" from
"the interface cannot reach the controller".

### Thermostats

A `climate` entity is created for a heating circuit **only if that circuit has a
room temperature sensor**. Without one, a thermostat showing a target and no
current temperature reads as broken rather than as a controller running on a
heating curve, so the circuit keeps a mode select and a setpoint number instead.

Discovery decides this and stores the answer; if it got it wrong, the **Room
sensors** option overrides it. HC2's thermostat needs the `advanced` write
level, because its registers are not among the basic seven.

### Blocks

There is no "hydraulic scheme" register, so which blocks exist is a heuristic:
a block is enabled if any of its read-only temperature or status registers
reported a non-zero, non-disabled value in either of the two setup reads.
Setpoints do not count — they hold a value whether or not the circuit is
plumbed in.

The heuristic is a default, not a verdict. Every block can be turned on or off
in the options.

---

## Polling

The interface board refreshes each value from the BSB bus on its own schedule,
and the register map states how often. Polling faster returns the same number
and only loads the bus, so the register map is the upper bound:

| Tier | Interval | Contents |
|---|---|---|
| Fast | 30 s | BSB link, heat pump status, flow and return, active errors |
| Normal | 120 s | Most measurements and the board's own counters |
| Slow | 300 s | Setpoints, heating curves, counters, fault history |
| Static | 1 h | Board identification, software versions |

The whole fast round is three requests. At 9600 baud that is well under 1 % bus
utilisation, which leaves room for other devices on the same bus.

Configured intervals are floored at each tier's own refresh rate; setting the
fast tier to 10 s gets you 30 s and a debug log line.

### After a power cut

The board needs about four minutes after power-up to refresh everything from the
BSB bus, and until then a read can legitimately answer 0. During the first five
minutes an exact 0 from a temperature register is published as unknown rather
than as 0 °C, per register, until that register reports a real value. Setup also
re-reads everything ten seconds after start-up for the same reason.

---

## Troubleshooting

### Diagnostics

**Settings → Devices & services → Fujitsu Waterstage → three-dot menu →
Download diagnostics.** The gateway address is redacted. The dump contains:

- board identification, uptime and serial number
- the BSB link status and the interface error code, resolved to text
- **every error counter**, split into Modbus and BSB, plus bus utilisation
- the last raw words of every read group, next to what they decoded to and
  whether the disable bit was set
- which blocks discovery excluded, and why

The counters are the useful part. Modbus counters climbing means the RS-485 side
is bad; BSB counters climbing means the cable between the board and the
controller is bad. Neither means the integration is bad.

### Reading the register map from the command line

`tools/dump.py` reads the whole map without Home Assistant. It needs `pymodbus`
and nothing else:

```bash
python tools/dump.py --host 192.168.1.37 --port 502 --slave 3
```

It prints the raw words, the decoded value, the disable flag and the register
name, and can emit JSON or CSV instead (`--format`). `--groups-only` shows which
requests would be made without connecting to anything.

### Common problems

| Symptom | Likely cause |
|---|---|
| "Something answered, but it is not an FWS-MBIO-002 board" | Wrong slave address, or the address of another device on the gateway |
| "did not answer with either framing" | The slave address is wrong, or another master is holding the gateway's only TCP connection — see below |
| Everything unavailable | The gateway or the board is not answering: check the network, the gateway, and the RS-485 wiring |
| Only the board's entities are available | The board is fine but cannot reach the controller: BSB wiring, or the heat pump is powered down |
| Temperatures unknown for a few minutes after a restart | Expected — see [After a power cut](#after-a-power-cut) |
| A value shows as unavailable and never comes back | The parameter is disabled in the controller. That is what the `/O` disable bit means; enable it from the controller's menu |

Many cheap gateways accept only one or two TCP connections at a time. If Home
Assistant is already polling the same gateway from a `modbus:` YAML
configuration, that connection is taken. Stop the other integration briefly to
tell a connection limit apart from a wiring or addressing problem.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The test suite runs without hardware: a fake Modbus client stands in for the
board, while the gateway, the read grouping, the codec and the entities are all
the real ones.

Home Assistant does not support Windows, and its test harness fails to import
there; `tests/win_compat.py` patches around that and does nothing on Linux and
macOS.

`tools/dump.py` is the only thing that needs real hardware, and it is also the
fastest way to check whether a problem is in the register map or in the code.

---

## Credits and licence

The register map in `docs/mbio_registers.json` was transcribed from the
*Waterstage FWS-MBIO-002 és FWS-RB-002* user manual (V2.1 revB) published by
ACITECH Solutions Kft. The manual is the authoritative source; where this
integration and the manual disagree, the manual is right and this is a bug.

`docs/DESIGN.md` is the design document the implementation follows, including
the parts that were deliberately left out and why.

Licensed under the terms in [LICENSE](LICENSE).
