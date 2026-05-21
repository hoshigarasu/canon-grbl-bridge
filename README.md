

https://github.com/user-attachments/assets/2cb020cb-481f-4602-aa36-f6fba25e7d6d

# canon-grbl-bridge

**A Python bridge that connects the `rs274ngc` G-code interpreter to grblHAL over serial.**

`rs274ngc` (the G-code engine from LinuxCNC) parses G-code and emits abstract *canon calls*
(`STRAIGHT_FEED`, `ARC_FEED`, `SPINDLE_ON`, …). This bridge intercepts those calls and
translates them into grblHAL commands (`G0`/`G1`/`G2`/`G3`/`M3`/…), streaming them over
a serial port with `ok`-based flow control.

The result: a standards-compliant G-code interpreter (canned cycles, subroutines,
coordinate systems, O-word loops) running on a Linux SoC, driving a grblHAL motion
controller over UART — with no Mesa card and no real-time kernel requirement.

---

## Architecture

```
G-code file (.ngc)
      │
      ▼
 rs274ngc interpreter          ← linuxcnc-uspace package
 (gcode.parse / Python API)
      │ canon calls
      ▼
 GrblBridge (this project)
      │  G0 / G1 / G2 / G3
      │  M3 / M5 / G4 / M2 …
      ▼
 /dev/ttyHS1  (115200 baud)    ← LPUART1 internal link on Arduino UNO Q
      │
      ▼
 grblHAL on STM32U585          ← grblHAL-STM32U585
      │
      ▼
 Stepper drivers / CNC shield
```

### What rs274ngc handles (no bridge code needed)

- Canned cycles (G81, G82, G83, …) — expanded to primitive moves
- O-word subroutines and loops
- G90/G91 absolute/incremental conversion
- G5x / G92 coordinate system offsets
- Polar coordinates (`@r ^θ`)
- Error checking and line numbering

### What the bridge handles

- Canon call → grblHAL command translation
- I/J arc center offset calculation (absolute center → offset from current position)
- `ok` / `error:N` flow control (one command in flight at a time)
- `[MSG:...]` passthrough without false error triggering
- Dry-run mode for offline verification

---

## Requirements

### On the Linux SoC (QRB2210 / Debian 13)

```bash
# linuxcnc-uspace provides rs274ngc Python bindings
sudo apt-get install -y linuxcnc-uspace

# pyserial for serial communication
pip3 install pyserial
```

Verify:

```bash
python3 -c "import rs274, gcode, linuxcnc; print('OK')"
```

### grblHAL firmware

[grblHAL-STM32U585](https://github.com/hoshigarasu/grblHAL-STM32U585) running on the
STM32U585, reachable via `/dev/ttyHS1` at 115200 baud.

---

## Installation

```bash
git clone https://github.com/hoshigarasu/canon-grbl-bridge.git
cd canon-grbl-bridge
```

No build step. The bridge is a single Python script.

---

## Usage

### Dry-run (no hardware needed)

Parses the G-code file and prints the grblHAL commands that would be sent:

```bash
python3 rs274ngc_grbl_bridge.py --dry-run path/to/program.ngc
```

Example output:

```
[DRY] G17
[DRY] G0 X1.5000 Y0.0000 Z0.0000
[DRY] G1 X0.7500 Y1.2990 Z-0.4000 F100.00
[DRY] G2 X1.6133 Y-1.1787 Z-0.1000 I-1.7127 J1.0288 F24.00
...
```

### Real execution

```bash
# Stop arduino-router and enable level shifter first
ssh -t uno-q 'sudo systemctl stop arduino-router.service && \
  sudo gpioset -c /dev/gpiochip1 -t0 70=1'

# Run
sudo python3 rs274ngc_grbl_bridge.py path/to/program.ngc
```

### Options

```
usage: rs274ngc_grbl_bridge.py [-h] [--port PORT] [--baud BAUD] [--dry-run] gcode_file

positional arguments:
  gcode_file    G-code file to execute (.ngc)

options:
  --port PORT   Serial port (default: /dev/ttyHS1)
  --baud BAUD   Baud rate   (default: 115200)
  --dry-run     Print commands without sending to hardware
```

---

## Canon call mapping

| rs274ngc canon call | grblHAL command |
|--------------------|-----------------|
| `straight_traverse(x,y,z,…)` | `G0 X.. Y.. Z..` |
| `straight_feed(x,y,z,…)` | `G1 X.. Y.. Z.. F..` |
| `arc_feed(x1,y1,cx,cy,rot,z1,…)` | `G2/G3 X.. Y.. Z.. I.. J.. F..` |
| `set_feed_rate(rate)` | embedded in next G1/G2/G3 |
| `set_plane(1/2/3)` | `G17` / `G18` / `G19` |
| `set_distance_mode(0/1)` | `G90` / `G91` |
| `spindle_on(rpm)` | `M3 S..` |
| `spindle_off()` | `M5` |
| `mist_on()` / `flood_on()` | `M7` / `M8` |
| `mist_off()` / `flood_off()` | `M9` |
| `dwell(seconds)` | `G4 P..` (milliseconds) |
| `program_end()` | `M2` |

Arc center: `cx`, `cy` from rs274ngc are absolute coordinates.
The bridge converts them to grblHAL's I/J offset format:
`I = cx − current_x`, `J = cy − current_y`.

---

## Flow control

grblHAL accepts one command at a time and responds with `ok` or `error:N`.
The bridge blocks on each `ok` before sending the next command, which naturally
throttles feed to match the planner buffer. `[MSG:...]` lines from grblHAL are
logged to stderr and do not interrupt the flow.

```
bridge              grblHAL
  │─── G1 X10 F500 ──▶│
  │◀────── ok ─────────│
  │─── G2 X0 Y10 … ──▶│
  │◀────── ok ─────────│
  │         …
```

---

## Parameter file

`rs274ngc` maintains a `.var` file to persist G-code parameters (G92 offsets, user
variables `#1`–`#5400`) across runs. The bridge creates a temporary file for each
run; parameters do not persist between invocations by default.

---

## Limitations

- **No position feedback**: the bridge tracks commanded position internally.
  If grblHAL loses steps, the bridge does not know.
- **No feed hold / resume**: real-time grblHAL commands (`!`, `~`, `?`) are not
  implemented. Stop with `Ctrl-C`; grblHAL will finish its planner buffer.
- **Single stream**: only one G-code file runs at a time.
- **Units**: the bridge initializes `rs274ngc` in mm (`G21`). The interpreter
  converts all coordinates internally, so G-code files using `G20` (inch) work
  correctly — all canon calls arrive in mm.

---

## Roadmap

The current bridge covers the core motion path. Several directions are planned
as the hardware platform (Arduino UNO Q underside connectors) expands:

### Position feedback
The STM32U585 has hardware quadrature decoder channels accessible via the UNO Q's
underside expansion ports. Once the firmware exposes encoder counts over `ttyHS1`,
the bridge can read actual position after each move and detect stalls — closing
the loop between commanded and actual position.

### Feed hold / resume / real-time status
grblHAL's real-time command bytes (`!` feed hold, `~` resume, `?` status query)
are single-byte commands that bypass the line buffer. The bridge will add a
background thread to handle `Ctrl-C` gracefully, query machine state during
long moves, and relay status back to the caller.

### 6-axis support
The UNO Q underside connectors provide enough GPIO for three additional
step/dir pairs beyond the CNC Shield V3's XYZ. When the firmware is extended
to support A/B/C axes, the bridge canon layer will map the corresponding
rotary canon calls (`angular_feed`, etc.) to the new axes.

### Trinamic driver integration
SPI/UART access to Trinamic stepper drivers (TMC2209, TMC5160) via the underside
ports would allow the bridge to command current scaling, stealthChop, and
stallGuard thresholds dynamically through G-code user M-codes.

---

## Companion project

[grblHAL-STM32U585](https://github.com/hoshigarasu/grblHAL-STM32U585) —
grblHAL firmware for the STM32U585 on the Arduino UNO Q, including the
LPUART1 port, VDDIO2 fix, LPUART BRR formula, and flash settings persistence.

---

## License

GPLv3 — same as grblHAL and LinuxCNC.
See [LICENSE](LICENSE) for details.

---

## Rev.4 — WebUI Integration (grbl_lcnc_gateway.py)

Rev.4 adds a WebSocket gateway that connects the
[lcnc-suite](https://github.com/bildobodo/lcnc-suite) web frontend to the
rs274ngc bridge and grblHAL backend.

```
lcnc-webui (Vue 3 — lcnc-suite)
      │  WebSocket JSON (lcnc-suite protocol)
      ▼
grbl_lcnc_gateway.py          ← this file
  ├─ status 30 Hz push        ← grblHAL ? polling
  ├─ viewer_gcode             ← rs274ngc dry-run → toolpath preview
  ├─ cycle_start / auto_step  ← rs274ngc → grblHAL streaming
  ├─ jog (cont / incr / diag) ← $J= commands
  ├─ WCS (G54–G59)            ← MDI passthrough with P0→Pn conversion
  └─ Spindle / Coolant        ← M3/M4/M5/M7/M8/M9
      │  ttyHS1 (115200 baud)
      ▼
grblHAL on STM32U585
```

### Additional requirements

```bash
# On QRB2210 (Debian 13 / Python 3.13)
pip3 install fastapi uvicorn python-multipart gpiod --break-system-packages
```

### Build lcnc-webui (run once on a development machine with Node.js ≥ 20)

```bash
git clone https://github.com/bildobodo/lcnc-suite.git
cd lcnc-suite/lcnc-webui
npm install
npm run build
# Copy dist/ to QRB2210
scp -r dist/ uno-q:~/canon-grbl-bridge/lcnc-webui/dist/
```

### Running as a systemd service (recommended)

```bash
sudo cp grbl-lcnc-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable grbl-lcnc-gateway
sudo systemctl start grbl-lcnc-gateway
```

The service automatically stops `arduino-router.service` (Conflicts=) and
manages GPIO70 (level shifter enable) via the `gpiod` Python library.

### Manual start (for development / debugging)

```bash
# Stop conflicting services first
sudo systemctl stop arduino-router.service arduino-router-serial.service arduino-app-cli.service

cd ~/canon-grbl-bridge
python3 grbl_lcnc_gateway.py [--port /dev/ttyHS1] [--web-port 8000]
```

Then open `http://192.168.0.52:8000` in a browser.

### CoreXY note

This project uses a CoreXY pen plotter. The grblHAL firmware must be compiled
with `COREXY=1` (see [grblHAL-STM32U585](https://github.com/hoshigarasu/grblHAL-STM32U585))
**and** `COREXY=1` must be added to the STM32CubeIDE Preprocessor settings:

> Project Properties → C/C++ Build → Settings → MCU GCC Compiler → Preprocessor → Defined symbols

### Step execution note

`auto_step` (the Step button in lcnc-suite) is implemented as a simplified
single-command executor: each button press sends one grblHAL command from the
rs274ngc-generated command list. This does **not** correspond 1:1 to G-code
blocks — a single G2/G3 arc produces one grblHAL command, while
complex canned cycles may produce many.

