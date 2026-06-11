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
STM32U585, reachable via `/dev/ttyHS1` at 115200 baud. Flash it from the UNO Q with
`./install.sh --flash-firmware` — see [Firmware flashing](#firmware-flashing-swd).

---

## Installation

The Arduino UNO Q ships with factory firmware (Zephyr + RouterBridge) on its
STM32U585. To run this project you flash **grblHAL** onto the STM32 and install
the bridge + WebUI on the QRB2210 Linux side — `install.sh` does both.

### First install (factory-fresh Arduino UNO Q)

Debian 13 trixie / aarch64, as the `arduino` user:

```bash
git clone https://github.com/hoshigarasu/canon-grbl-bridge.git
cd canon-grbl-bridge
./install.sh --flash-firmware
```

`--flash-firmware` writes the bundled grblHAL image (`firmware/grblHAL_UNO_Q.elf`)
to the STM32U585 over SWD using the UNO Q's on-board OpenOCD. When it finishes it
asks you to **power-cycle the board once** — grblHAL only starts cleanly after a
power-on reset (see [Firmware flashing](#firmware-flashing-swd)). After the power
cycle the gateway service starts automatically; open `http://<UNO-Q-IP>:8000`.

The installer:
1. Installs system dependencies (linuxcnc-uspace, nodejs, npm, gpiod, …)
2. Installs Python packages (FastAPI, uvicorn, python-multipart, gpiod)
3. Clones / updates this repository
4. Creates persistent data directories (`~/ngc`, `~/.config/lcnc_gateway`)
5. **Disables `arduino-router`** — it claims `ttyHS1` with the RouterBridge
   protocol and conflicts with the grbl protocol (looks connected but won't move)
6. Registers and enables the `grbl-lcnc-gateway` systemd service
7. With `--flash-firmware`: flashes grblHAL over SWD, then prompts for a power cycle

### Software updates (no re-flash, no power cycle)

To update the bridge / gateway / WebUI without touching the STM32 firmware, run
the installer **without** the flag:

```bash
cd canon-grbl-bridge && git pull
./install.sh
```

Dependencies are reinstalled idempotently and the gateway is restarted; the STM32
is left untouched, so no power cycle is needed. The `--flash-firmware` step is
only for first install or an actual firmware change — and re-flashing always
needs one power cycle afterward.

### Firmware flashing (SWD)

grblHAL is flashed onto the STM32U585 from the QRB2210 itself — no external
programmer. The UNO Q carries an on-board OpenOCD (`/opt/openocd`) wired to the
STM32 SWD pins (gpiochip1: SWCLK=26, SWDIO=25, NRST=38). `--flash-firmware` runs:

```bash
sudo systemctl stop grbl-lcnc-gateway          # release ttyHS1
sudo /opt/openocd/bin/openocd \
  -s /opt/openocd -s /opt/openocd/share/openocd/scripts \
  -f /opt/openocd/openocd_gpiod.cfg \
  -c "init" -c "reset halt" \
  -c "flash write_image erase firmware/grblHAL_UNO_Q.elf" \
  -c "verify_image firmware/grblHAL_UNO_Q.elf" \
  -c "shutdown"
```

The committed `firmware/grblHAL_UNO_Q.elf` is the authoritative image (a CubeIDE
build; the file is large because it carries debug symbols, but OpenOCD writes
only the ~300 KB of loadable sections).

**Why a power cycle is required:** after `reset halt` the STM32U585 boot ROM (RSS)
is interrupted, and an OpenOCD warm reset (`reset run`) does not bring grblHAL
back — the CPU stays parked in the RSS and `ttyHS1` goes silent. The debug power
domain is cleared only by a power-on reset, so one physical power cycle is needed
after flashing. It is a one-time step per firmware write; software updates never
touch the STM32 and need no power cycle.

### Manual installation

```bash
git clone https://github.com/hoshigarasu/canon-grbl-bridge.git
cd canon-grbl-bridge
```

No build step for the bridge itself. The WebUI dist is included in the repository.

---

## Usage

### WebUI (normal operation)

Open `http://<uno-q-ip>:8000` in a browser. Upload an `.ngc` file, preview the
toolpath in 3D, and run. Mobile layout is available at `http://<uno-q-ip>:8000/?mobile`.

The installer permanently disables `arduino-router`, so `ttyHS1` is free for the
gateway. The gateway (`grbl-lcnc-gateway` systemd service) manages the level
shifter (GPIO70) automatically — nothing else to do.

### Smoke test

Verifies service, HTTP, WebSocket, grblHAL serial round-trip, and toolpath
parsing without moving the machine:

```bash
./smoke_test.sh                # no motion
./smoke_test.sh --with-motion  # adds a ±0.1 mm X jog
```

### WebUI deployment scripts

| Script | Runs on | Builds on | Use case |
|---|---|---|---|
| `deploy-webui.sh` | PC | UNO Q | Normal: push lcnc-suite, build & deploy on the box |
| `update-webui.sh` | UNO Q | UNO Q | Box-internal build + dist commit (called by deploy-webui.sh) |
| `update-webui-pc.sh` | PC | PC | Fallback for 2 GB boards where on-board npm build hits OOM |

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
  ├─ feed_hold / cycle_pause  ← grblHAL RT command ! (0x21)
  ├─ cycle_resume             ← grblHAL RT command ~ (0x7E)
  ├─ abort                    ← grblHAL RT command \x18
  ├─ jog (cont / incr / diag) ← $J= commands
  ├─ WCS (G54–G59)            ← MDI passthrough with P0→Pn conversion
  └─ Spindle / Coolant        ← M3/M4/M5/M7/M8/M9
      │  ttyHS1 (115200 baud)
      ▼
grblHAL on STM32U585
```

### Persistent data

| Purpose | Path |
|---------|------|
| NGC files | `/home/arduino/ngc/` |
| UI settings | `/home/arduino/.config/lcnc_gateway/settings.json` |

Both paths survive reboots. NGC files uploaded via the browser UI are stored in
`/home/arduino/ngc/` instead of `/tmp/`.

### Additional endpoints

| URL | Method | Description |
|-----|--------|-------------|
| `/editor` | GET | Popup G-code editor (CodeMirror 5, monokai) |
| `/active-file` | GET | Returns path of currently loaded NGC file |
| `/read-file?path=…` | GET | Returns raw content of an NGC file |
| `/save` | PUT | Save NGC file content |
| `/grbl-settings` | GET | grblHAL `$$` parameter viewer/editor |
| `/grbl-settings-data` | GET | Fetch all `$$` settings as JSON |
| `/grbl-settings-data` | POST | Set a single `$N=value` parameter |

### G-code popup editor

The **Edit** button in the Program panel opens a full-window popup editor
instead of the inline textarea. Features:

- CodeMirror 5 with monokai theme
- **Save** (Ctrl+S) — overwrites the current file
- **Save As…** — saves to `/home/arduino/ngc/` with filename prompt; `.ngc`
  extension added automatically if omitted
- **Reload** — discards edits and reloads from disk

### grblHAL settings UI

The **⚙** button (bottom-right corner) opens the grblHAL settings page at
`/grbl-settings`. It fetches all `$$` parameters from the hardware and displays
them in a filterable table. Each row is editable; clicking **Set** sends
`$N=value` to grblHAL immediately.

### Additional requirements

```bash
# On QRB2210 (Debian 13 / Python 3.13)
pip3 install fastapi uvicorn python-multipart gpiod --break-system-packages
```

### Updating the WebUI

The pre-built `lcnc-webui/dist/` is included in this repository. To rebuild
from the latest [lcnc-suite](https://github.com/hoshigarasu/lcnc-suite) source:

```bash
git clone https://github.com/hoshigarasu/lcnc-suite.git
cd lcnc-suite/lcnc-webui
npm install
npm run build
scp -r dist/ uno-q:~/canon-grbl-bridge/lcnc-webui/dist/
ssh uno-q 'sudo systemctl restart grbl-lcnc-gateway'
```

### Service management

```bash
sudo systemctl start   grbl-lcnc-gateway
sudo systemctl stop    grbl-lcnc-gateway
sudo systemctl restart grbl-lcnc-gateway
sudo journalctl -u grbl-lcnc-gateway -f
```

The service automatically stops `arduino-router.service` (Conflicts=) and
manages GPIO70 (level shifter enable) via the `gpiod` Python library.

### Manual start (for development / debugging)

```bash
sudo systemctl stop arduino-router.service arduino-router-serial.service arduino-app-cli.service

cd ~/canon-grbl-bridge
python3 grbl_lcnc_gateway.py [--port /dev/ttyHS1] [--web-port 8000]
```

Then open `http://<QRB2210-IP>:8000` in a browser.

### Operator workflow

1. Open `http://<IP>:8000` in a browser
2. Press **ARM**
3. Press **Off** (= machine_on → enabled)
4. Jog to verify tool position
5. **Upload** or **Browse** to load an NGC file
6. **Start** to run / **Step** for single-step / **Pause** to feed-hold
7. **Zero X/Y/Z** to set work origin
8. **⚙** (bottom-right) to view/edit grblHAL parameters

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

### Hardware caution

| Operation | Consequence |
|-----------|-------------|
| `gpioset 38=1` | Immediate QRB2210 shutdown |
| OpenOCD reset without stopping service | STM32 reset → QRB2210 shutdown |
| Unbinding ttyHS1 | Cannot rebind without reboot |
| Writing APB registers while halted | Silently ignored (clock gating) |

> Firmware flashing is handled safely by `./install.sh --flash-firmware`, which
> stops the gateway first and uses the verified SWD sequence above. Flash this
> way rather than poking GPIO/OpenOCD by hand. See
> [Firmware flashing](#firmware-flashing-swd).

### Homing note

This project runs with $22=0 (homing disabled) on a CoreXY pen plotter with no limit switches.
The gateway always reports homed: true so the UI treats the machine as permanently homed.
Home buttons are accepted as no-ops. To enable real homing, set $22=1 in grblHAL settings
and configure limit switch pins — the gateway will then need a home command handler that
sends $H to grblHAL.
