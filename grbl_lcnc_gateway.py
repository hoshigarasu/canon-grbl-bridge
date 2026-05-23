#!/usr/bin/env python3
"""
grbl_lcnc_gateway.py
lcnc-suite WebSocket プロトコルを実装する FastAPI ゲートウェイ。

Architecture:
    lcnc-webui (Vue 3)
        ↕ WebSocket JSON (lcnc-suite protocol)
    grbl_lcnc_gateway.py  ← このファイル
        ├─ StatusPoller: ?を100msポーリング → status 30Hz push
        ├─ viewer_gcode:  rs274ngcでパース → feed/rapid 座標列生成
        ├─ auto_run:      rs274ngc → GrblBridge → ttyHS1 非同期ストリーミング
        ├─ abort/pause/resume: リアルタイム制御文字
        └─ jog: $J= コマンド
        ↕ ttyHS1 115200baud
    grblHAL (STM32U585)

Usage:
    python3 grbl_lcnc_gateway.py [--port /dev/ttyHS1] [--web-port 8000]

Dependencies:
    pip3 install fastapi uvicorn pyserial --break-system-packages
    # rs274ngc: linuxcnc-uspace パッケージ (インストール済み)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import serial
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# rs274ngc (linuxcnc-uspace パッケージ)
try:
    import gcode
    from rs274.interpret import Translated, StatMixin
    RS274_AVAILABLE = True
except ImportError:
    RS274_AVAILABLE = False
    log.warning("rs274ngc not available — toolpath extraction disabled")

# ─────────────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────────────
DEFAULT_PORT    = "/dev/ttyHS1"
DEFAULT_BAUD    = 115200
SERIAL_TIMEOUT  = 10
STATUS_POLL_HZ  = 30          # status push レート
POLL_INTERVAL   = 1.0 / STATUS_POLL_HZ
GRBL_POLL_MS    = 100         # ? コマンド送信間隔 (ms)
MM_PER_INCH     = 25.4
INITCODE        = "G17 G40 G49 G80 G90"
NGC_UPLOAD_DIR  = Path("/home/arduino/ngc")
WEBUI_DIST      = Path(__file__).parent / "lcnc-webui" / "dist"

# lcnc.ts 定数
INTERP_IDLE     = 1
INTERP_READING  = 2
INTERP_PAUSED   = 3
INTERP_WAITING  = 4
TASK_MODE_MANUAL = 1
TASK_MODE_AUTO   = 2
TASK_MODE_MDI    = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

# ─────────────────────────────────────────────────────────────────────
# グローバル状態
# ─────────────────────────────────────────────────────────────────────
class MachineState:
    """スレッド間共有の機械状態。ロックで保護。"""
    def __init__(self):
        self.lock = threading.Lock()
        # grblHAL ? レスポンスから更新
        self.grbl_state  = "Disconnected"  # Idle / Run / Hold / Alarm / ...
        self.mpos        = [0.0, 0.0, 0.0]
        self.wpos        = [0.0, 0.0, 0.0]
        self.feed        = 0.0
        self.spindle_rpm = 0.0
        # gateway 管理
        self.active_file : Optional[str] = None
        self.task_mode   = TASK_MODE_MANUAL
        self.interp_state = INTERP_IDLE
        self.feed_override = 1.0
        self.estop       = False
        self.enabled     = False
        self.armed       = False
        self.flood       = False
        self.mist        = False
        self.spindle_direction = 0
        self.probe_tripped = False
        self.wcs_p       = 1  # 現在のWCS: G54=1, G55=2, ..., G59=6
        # ステップ実行用
        self.step_commands: list[str] = []
        self.step_index: int = 0
        # 実行スレッド管理
        self.run_task: Optional[asyncio.Task] = None

    def to_status_data(self) -> dict:
        with self.lock:
            return {
                "ts":               time.time(),
                "armed":            self.armed,
                "estop":            self.estop,
                "enabled":          self.enabled,
                "homed":            True,           # grblHAL $22=0 ホーミング無効→常にhomed扱い
                "task_mode":        self.task_mode,
                "interp_state":     self.interp_state,
                "state":            1,
                "machine_pos":      list(self.mpos),
                "work_pos":         list(self.wpos),
                "joint_pos":        list(self.mpos),
                "g5x_offset":       [0.0, 0.0, 0.0],
                "g92_offset":       [0.0, 0.0, 0.0],
                "tool_offset":      [0.0, 0.0, 0.0],
                "dtg":              [0.0, 0.0, 0.0],
                "feed_override":    self.feed_override,
                "spindle_override":  1.0,
                "spindle_speed":     self.spindle_rpm,
                "spindle_speed_actual": self.spindle_rpm,
                "spindle_direction": self.spindle_direction,
                "rapid_override":    1.0,
                "max_velocity":      5000.0,
                "current_vel":       self.feed,
                "active_file":       self.active_file or "",
                "motion_line":       0,
                "tool_number":       0,
                "tool_diameter":     0.0,
                "tool_length":       0.0,
                "flood":             self.flood,
                "mist":              self.mist,
                "probe_tripped":     self.probe_tripped,
                "probing":           False,
                "probed_position":   [0.0, 0.0, 0.0],
            }

machine = MachineState()

# ─────────────────────────────────────────────────────────────────────
# シリアル通信レイヤー（スレッドセーフ）
# ─────────────────────────────────────────────────────────────────────
class SerialBus:
    """
    ttyHS1 との排他的シリアル通信。
    ・send_rt()  : リアルタイム制御文字（ロックなし即時送信）
    ・send_cmd() : okベースのコマンド送信（送受信ロック付き）
    ・poll_status(): ? ポーリング → MachineState 更新
    """
    def __init__(self, port: str, baud: int):
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self._cmd_lock = threading.Lock()
        log.info(f"Serial opened: {port} @ {baud}")

    def send_rt(self, byte: bytes):
        """リアルタイム制御文字を即時送信（Feed Hold, Resume, Reset）"""
        self.ser.write(byte)

    def send_jog(self, cmd: str):
        """jogコマンドを即時送信（ロックなし・okを待たない）"""
        log.info(f"JOG TX: {cmd!r}")
        self.ser.write((cmd.strip() + "\n").encode())

    def _drain_input(self, timeout=0.05):
        """未読バッファを空読みして捨てる（jogのokが残っていた場合の対策）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                self.ser.readline()
            else:
                break

    def send_cmd(self, cmd: str) -> str:
        """コマンド1行を送信し ok/error を待つ。戻り値は最終応答行。"""
        with self._cmd_lock:
            self._drain_input()   # jog ok 残りをクリア
            line = cmd.strip()
            self.ser.write((line + "\n").encode())
            return self._wait_ok(line)

    def _wait_ok(self, sent: str = "") -> str:
        deadline = time.time() + SERIAL_TIMEOUT
        while time.time() < deadline:
            resp = self.ser.readline().decode(errors="replace").strip()
            if not resp:
                continue
            if resp.startswith("["):
                log.debug(f"[grbl msg] {resp}")
                continue
            if resp.startswith("ALARM"):
                log.warning(f"grblHAL ALARM: {resp!r} (sent: {sent!r})")
                with machine.lock:
                    machine.grbl_state = "Alarm"
                    machine.estop = True
                return resp
            if resp.startswith("error"):
                log.warning(f"grblHAL error: {resp!r} (sent: {sent!r})")
                return resp
            if resp == "ok":
                return "ok"
            log.debug(f"[recv] {resp!r}")
        raise TimeoutError(f"grblHAL no response for: {sent!r}")

    def send_and_collect(self, cmd: str) -> list:
        """コマンドを送信し $始まりの複数行を収集してokを待つ（$$ 用）"""
        with self._cmd_lock:
            self._drain_input()
            self.ser.write((cmd.strip() + "\n").encode())
            lines = []
            deadline = time.time() + SERIAL_TIMEOUT
            while time.time() < deadline:
                resp = self.ser.readline().decode(errors="replace").strip()
                if not resp:
                    continue
                if resp == "ok":
                    return lines
                if resp.startswith("error") or resp.startswith("ALARM"):
                    raise RuntimeError(resp)
                if resp.startswith("$"):
                    lines.append(resp)
                else:
                    log.debug(f"[collect] {resp!r}")
            raise TimeoutError(f"no response for: {cmd!r}")

    def poll_status(self):
        """? を送信して MachineState を更新。ブロッキング（短い）。"""
        with self._cmd_lock:
            self.ser.write(b"?")
            deadline = time.time() + 0.3
            while time.time() < deadline:
                resp = self.ser.readline().decode(errors="replace").strip()
                if resp.startswith("<") and resp.endswith(">"):
                    _parse_grbl_status(resp)
                    return

    def close(self):
        if self.ser.is_open:
            self.ser.close()


def _parse_grbl_status(resp: str):
    """
    <Idle|MPos:0.000,0.000,0.000|WPos:0.000,0.000,0.000|Bf:99,1023|F:1000|S:0>
    → MachineState 更新
    """
    inner = resp[1:-1]
    parts = inner.split("|")
    with machine.lock:
        machine.grbl_state = parts[0]  # Idle / Run / Hold / Alarm / ...

        # interp_state / task_mode を grbl state から推定
        state = parts[0]
        if state == "Idle":
            machine.interp_state = INTERP_IDLE
            if machine.task_mode == TASK_MODE_AUTO:
                # プログラム終了後に手動モードへ戻す
                machine.task_mode = TASK_MODE_MANUAL
        elif state == "Run":
            machine.interp_state = INTERP_READING
            machine.task_mode = TASK_MODE_AUTO
        elif state in ("Hold:0", "Hold:1", "Hold"):
            machine.interp_state = INTERP_PAUSED
        elif state.startswith("Alarm"):
            machine.estop = True

        for p in parts[1:]:
            if p.startswith("MPos:"):
                vals = p[5:].split(",")
                machine.mpos = [float(v) for v in vals[:3]]
            elif p.startswith("WPos:"):
                vals = p[5:].split(",")
                machine.wpos = [float(v) for v in vals[:3]]
            elif p.startswith("F:"):
                sub = p[2:].split(",")
                machine.feed = float(sub[0])
                if len(sub) > 1:
                    machine.spindle_rpm = float(sub[1])
            elif p.startswith("FS:"):
                sub = p[3:].split(",")
                machine.feed = float(sub[0])
                if len(sub) > 1:
                    machine.spindle_rpm = float(sub[1])
            elif p.startswith("A:"):
                # 補助ステート: S=SpindleCW, C=SpindleCCW, F=Flood, M=Mist
                flags = p[2:]
                machine.spindle_rpm = machine.spindle_rpm  # 変更なし
                if "S" in flags:
                    machine.spindle_direction = 1
                elif "C" in flags:
                    machine.spindle_direction = -1
                else:
                    machine.spindle_direction = 0
                machine.flood = "F" in flags
                machine.mist  = "M" in flags
            elif p.startswith("Pn:"):
                # ピン状態: X/Y/Z=リミット, P=プローブ, D=ドア, H=ホールド, R=ソフトリセット, S=サイクルスタート
                pins = p[3:]
                machine.probe_tripped = "P" in pins


serial_bus: Optional[SerialBus] = None  # startup で初期化

# ─────────────────────────────────────────────────────────────────────
# rs274ngc bridge（GrblBridge）
# ─────────────────────────────────────────────────────────────────────
class FakeStat:
    tool_table    = ()
    angular_units = 1.0
    linear_units  = 1.0
    axis_mask     = 0b111
    block_delete  = False


class GrblBridge(Translated, StatMixin):
    """
    canon コール → grblHAL コマンド変換。
    dry_run=True の場合はシリアル送信せずツールパス座標を収集する（viewer_gcode用）。
    """

    def __init__(self, dry_run=False, abort_event: Optional[threading.Event] = None,
                 collect_commands=False):
        stat = FakeStat()
        StatMixin.__init__(self, stat, 0)
        self.dry_run          = dry_run
        self.collect_commands = collect_commands
        self.commands: list[str] = []   # collect_commands=Trueのとき収集
        self.abort_event  = abort_event or threading.Event()
        self.feed_rate    = 0.0
        self.current_x    = 0.0
        self.current_y    = 0.0
        self.current_z    = 0.0
        self.parameter_file = ""
        # viewer_gcode 用のツールパス収集
        self.path_feed:   list[list[float]] = []
        self.path_rapid:  list[list[float]] = []

    def _send(self, cmd: str):
        if self.abort_event.is_set():
            raise RuntimeError("Aborted")
        if self.collect_commands:
            self.commands.append(cmd)
            return
        if self.dry_run:
            return
        serial_bus.send_cmd(cmd)

    def _update_pos(self, x, y, z):
        self.current_x, self.current_y, self.current_z = x, y, z

    def get_external_length_units(self):
        return 25.4  # inch → mm

    # ── Translated 経由の移動 ──────────────────────────────────────

    def straight_traverse_translated(self, x, y, z, a, b, c, u, v, w):
        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH
        self.path_rapid.append([round(x, 4), round(y, 4), round(z, 4)])
        self._send(f"G0 X{x:.4f} Y{y:.4f} Z{z:.4f}")
        self._update_pos(x, y, z)

    def straight_feed_translated(self, x, y, z, a, b, c, u, v, w):
        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH
        self.path_feed.append([round(x, 4), round(y, 4), round(z, 4)])
        self._send(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F{self.feed_rate:.2f}")
        self._update_pos(x, y, z)

    # ── 円弧（Translated を経由しない）──────────────────────────────

    def arc_feed(self, x1, y1, cx, cy, rot, z1, a, b, c, u, v, w):
        x1 *= MM_PER_INCH; y1 *= MM_PER_INCH; z1 *= MM_PER_INCH
        cx *= MM_PER_INCH; cy *= MM_PER_INCH
        i = cx - self.current_x
        j = cy - self.current_y
        g = "G3" if rot > 0 else "G2"
        # viewer 用：弧を近似線分に分解して追加
        if self.dry_run:
            self._arc_to_segments(x1, y1, z1, cx, cy, rot)
        self.path_feed.append([round(x1, 4), round(y1, 4), round(z1, 4)])
        self._send(
            f"{g} X{x1:.4f} Y{y1:.4f} Z{z1:.4f} "
            f"I{i:.4f} J{j:.4f} F{self.feed_rate:.2f}"
        )
        self._update_pos(x1, y1, z1)

    def _arc_to_segments(self, x1, y1, z1, cx, cy, rot, n=16):
        """弧を n 分割してpath_feedに追加（プレビュー精度向上）"""
        import math
        r0 = math.atan2(self.current_y - cy, self.current_x - cx)
        r1 = math.atan2(y1 - cy, x1 - cx)
        radius = math.hypot(self.current_x - cx, self.current_y - cy)
        if rot > 0:  # CCW
            if r1 <= r0:
                r1 += 2 * math.pi
        else:        # CW
            if r1 >= r0:
                r1 -= 2 * math.pi
        for i in range(1, n):
            t = i / n
            angle = r0 + (r1 - r0) * t
            xp = cx + radius * math.cos(angle)
            yp = cy + radius * math.sin(angle)
            zp = self.current_z + (z1 - self.current_z) * t
            self.path_feed.append([round(xp, 4), round(yp, 4), round(zp, 4)])

    # ── その他 canon コール ──────────────────────────────────────────

    def set_feed_rate(self, rate):
        self.feed_rate = rate * MM_PER_INCH

    def dwell(self, seconds):
        self._send(f"G4 P{int(seconds * 1000)}")

    def spindle_on(self, speed, *args):
        self._send(f"M3 S{speed:.0f}")

    def spindle_off(self):
        self._send("M5")

    def mist_on(self):    self._send("M7")
    def flood_on(self):   self._send("M8")
    def mist_off(self):   self._send("M9")
    def flood_off(self):  self._send("M9")

    def set_plane(self, plane):
        planes = {1: "G17", 2: "G18", 3: "G19"}
        if plane in planes:
            self._send(planes[plane])

    def program_end(self):
        self._send("M2")

    def check_abort(self):
        return self.abort_event.is_set()

    def next_line(self, state):
        self.state = state

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        log.debug(f"[canon] unimplemented: {name}()")
        return lambda *args, **kwargs: None


def run_ngc_in_thread(ngc_path: str, abort_event: threading.Event,
                      on_done: callable, dry_run=False) -> threading.Thread:
    """
    GrblBridgeをスレッドで実行。
    on_done(success: bool, error: str) をスレッド終了時に呼ぶ。
    """
    def _run():
        td = tempfile.mkdtemp()
        try:
            var_file = os.path.join(td, "bridge.var")
            default_var = "/usr/share/linuxcnc/ncfiles/linuxcnc.var"
            if os.path.exists(default_var):
                shutil.copy(default_var, var_file)
            else:
                open(var_file, "w").close()

            bridge = GrblBridge(dry_run=dry_run, abort_event=abort_event)
            bridge.parameter_file = var_file

            result, seq = gcode.parse(ngc_path, bridge, "G21", INITCODE)
            if result > gcode.MIN_ERROR:
                on_done(False, f"G-code parse error at line {seq}: code {result}")
            else:
                on_done(True, "")
            return bridge  # caller が path_feed/path_rapid を参照できるよう返す
        except Exception as e:
            on_done(False, str(e))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ─────────────────────────────────────────────────────────────────────
# ツールパス抽出（viewer_gcode 用）
# ─────────────────────────────────────────────────────────────────────
def extract_toolpath(ngc_path: str) -> tuple[list, list]:
    """
    dry_run モードで GrblBridge を実行し、feed/rapid 座標列を返す。
    ブロッキング（呼び出しはスレッドで行うこと）。
    """
    td = tempfile.mkdtemp()
    try:
        var_file = os.path.join(td, "bridge.var")
        default_var = "/usr/share/linuxcnc/ncfiles/linuxcnc.var"
        if os.path.exists(default_var):
            shutil.copy(default_var, var_file)
        else:
            open(var_file, "w").close()

        abort = threading.Event()
        bridge = GrblBridge(dry_run=True, abort_event=abort)
        bridge.parameter_file = var_file

        result, seq = gcode.parse(ngc_path, bridge, "G21", INITCODE)
        if result > gcode.MIN_ERROR:
            log.warning(f"Toolpath extract: G-code error at line {seq}")

        return bridge.path_feed, bridge.path_rapid
    finally:
        shutil.rmtree(td, ignore_errors=True)


def extract_commands(ngc_path: str) -> list[str]:
    """
    NGCをパースしてgrblHALコマンドリストを生成（ステップ実行用）。
    NOTE: 簡易実装 — rs274ngcの1ブロック≠grblコマンド1個の場合あり。
    """
    td = tempfile.mkdtemp()
    try:
        var_file = os.path.join(td, "bridge.var")
        default_var = "/usr/share/linuxcnc/ncfiles/linuxcnc.var"
        if os.path.exists(default_var):
            shutil.copy(default_var, var_file)
        else:
            open(var_file, "w").close()

        abort = threading.Event()
        bridge = GrblBridge(collect_commands=True, abort_event=abort)
        bridge.parameter_file = var_file
        gcode.parse(ngc_path, bridge, "G21", INITCODE)
        return bridge.commands
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────
class ClientSet:
    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket):
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, msg: dict):
        text = json.dumps(msg)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)


clients = ClientSet()

# ─────────────────────────────────────────────────────────────────────
# Status ポーリングループ（asyncio タスク）
# ─────────────────────────────────────────────────────────────────────
async def status_loop():
    """
    grblHAL に ? を送って MachineState を更新し、
    30Hz で全クライアントに status をブロードキャスト。
    """
    loop = asyncio.get_event_loop()
    poll_interval = GRBL_POLL_MS / 1000.0
    next_poll = time.monotonic()

    while True:
        now = time.monotonic()
        if now >= next_poll and serial_bus is not None:
            try:
                await loop.run_in_executor(None, serial_bus.poll_status)
            except Exception as e:
                log.debug(f"poll_status error: {e}")
            next_poll = time.monotonic() + poll_interval

        await clients.broadcast({
            "type": "status",
            "data": machine.to_status_data(),
            "errors": [],
            "clients": [],
        })
        await asyncio.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────
# コマンドハンドラ
# ─────────────────────────────────────────────────────────────────────
_abort_event = threading.Event()


async def handle_command(ws: WebSocket, msg: dict) -> dict:
    """
    クライアントからのコマンドを処理し、reply dict を返す。
    """
    cmd = msg.get("cmd", "")
    log.info(f"CMD: {cmd} {dict((k,v) for k,v in msg.items() if k != 'cmd')}")
    loop = asyncio.get_event_loop()

    # ── heartbeat / arm ──────────────────────────────────────────
    if cmd == "heartbeat":
        return {"type": "pong"}

    if cmd == "tab_visibility":
        return {"type": "reply", "ok": True}

    if cmd == "arm":
        armed = msg.get("armed", True)
        with machine.lock:
            machine.armed = armed
        return {"type": "reply", "ok": True, "armed": armed}

    # ── 機械電源 / ESTOP ─────────────────────────────────────────
    if cmd == "machine_on":
        if serial_bus:
            try:
                await loop.run_in_executor(None, serial_bus.send_cmd, "$X")
            except Exception as e:
                log.warning(f"machine_on $X error: {e}")
        with machine.lock:
            machine.estop   = False
            machine.enabled = True
        return {"type": "reply", "ok": True}

    if cmd == "machine_off":
        # Machine Off: 非常停止ではなくモーター無効化
        # grblHALにはSoft Resetを送らない（E-STOPにしない）
        with machine.lock:
            machine.enabled = False
        return {"type": "reply", "ok": True}

    if cmd in ("estop",):
        # 真のE-STOP: Soft Reset + estop状態
        if serial_bus:
            serial_bus.send_rt(b"\x18")
        with machine.lock:
            machine.estop   = True
            machine.enabled = False
        return {"type": "reply", "ok": True}

    if cmd == "estop_reset":
        if serial_bus:
            await loop.run_in_executor(None, serial_bus.send_cmd, "$X")
        with machine.lock:
            machine.estop = False
        return {"type": "reply", "ok": True}

    # ── プログラム実行 ────────────────────────────────────────────
    if cmd in ("auto_run", "cycle_start"):
        with machine.lock:
            ngc = machine.active_file
        if not ngc or not os.path.exists(ngc):
            return {"type": "reply", "ok": False, "error": "No file loaded"}

        _abort_event.clear()

        def on_done(success, error):
            with machine.lock:
                machine.interp_state = INTERP_IDLE
                machine.task_mode    = TASK_MODE_MANUAL
            if not success:
                log.warning(f"auto_run failed: {error}")

        with machine.lock:
            machine.interp_state = INTERP_READING
            machine.task_mode    = TASK_MODE_AUTO

        run_ngc_in_thread(ngc, _abort_event, on_done, dry_run=(serial_bus is None))
        return {"type": "reply", "ok": True}

    if cmd == "auto_step":
        with machine.lock:
            ngc = machine.active_file
            # 初回またはファイル変更時にコマンドリストを生成
            if not machine.step_commands and ngc and os.path.exists(ngc):
                machine.interp_state = INTERP_READING
        if not ngc:
            return {"type": "reply", "ok": False, "error": "No file loaded"}

        with machine.lock:
            if not machine.step_commands:
                # コマンドリストをバックグラウンドで生成
                try:
                    cmds = await loop.run_in_executor(None, extract_commands, ngc)
                    machine.step_commands = cmds
                    machine.step_index    = 0
                    log.info(f"auto_step: {len(cmds)} commands prepared")
                except Exception as e:
                    return {"type": "reply", "ok": False, "error": str(e)}

            idx   = machine.step_index
            cmds  = machine.step_commands
            total = len(cmds)

        if idx >= total:
            # 全コマンド完了
            with machine.lock:
                machine.step_commands = []
                machine.step_index    = 0
                machine.interp_state  = INTERP_IDLE
                machine.task_mode     = TASK_MODE_MANUAL
            log.info("auto_step: completed")
            return {"type": "reply", "ok": True}

        cmd_to_send = cmds[idx]
        with machine.lock:
            machine.step_index = idx + 1

        if serial_bus and cmd_to_send:
            try:
                await loop.run_in_executor(None, serial_bus.send_cmd, cmd_to_send)
                log.info(f"auto_step [{idx+1}/{total}]: {cmd_to_send!r}")
            except Exception as e:
                return {"type": "reply", "ok": False, "error": str(e)}

        return {"type": "reply", "ok": True}
    if cmd == "cycle_pause" or cmd == "feed_hold":
        if serial_bus:
            serial_bus.send_rt(b"!")  # Feed Hold
        with machine.lock:
            machine.interp_state = INTERP_PAUSED
        return {"type": "reply", "ok": True}

    if cmd == "cycle_resume":
        if serial_bus:
            serial_bus.send_rt(b"~")  # Cycle Start / Resume
        with machine.lock:
            machine.interp_state = INTERP_READING
        return {"type": "reply", "ok": True}

    if cmd == "abort":
        _abort_event.set()
        if serial_bus:
            serial_bus.send_rt(b"\x18")  # Soft Reset
        with machine.lock:
            machine.interp_state  = INTERP_IDLE
            machine.task_mode     = TASK_MODE_MANUAL
            machine.step_commands = []
            machine.step_index    = 0
        return {"type": "reply", "ok": True}

    # ── MDI ──────────────────────────────────────────────────────
    if cmd == "mdi":
        text = msg.get("text", "").strip()

        # WCS切り替えを追跡（G54=P1...G59=P6）
        _wcs_map = {"G54":1,"G55":2,"G56":3,"G57":4,"G58":5,"G59":6,
                    "G59.1":7,"G59.2":8,"G59.3":9}
        if text in _wcs_map:
            with machine.lock:
                machine.wcs_p = _wcs_map[text]

        # LinuxCNC固有表現をgrblHAL互換に変換
        with machine.lock:
            p_cur = machine.wcs_p
        text = re.sub(r'\bP0\b', f'P{p_cur}', text)  # P0→現在WCS番号
        if "O<go_to_zero>" in text:
            text = "G0 X0 Y0 Z0"
        elif "O<go_to_g30>" in text:
            text = "G30"
        elif "O<go_to_home>" in text:
            text = ""  # ホーミング無効のためno-op

        if serial_bus and text:
            try:
                await loop.run_in_executor(None, serial_bus.send_cmd, text)
            except Exception as e:
                return {"type": "reply", "ok": False, "error": str(e)}
        with machine.lock:
            machine.task_mode = TASK_MODE_MANUAL
        return {"type": "reply", "ok": True}

    # ── ジョグ ───────────────────────────────────────────────────
    if cmd == "jog_cont":
        axis = msg.get("axis", 0)
        vel  = float(msg.get("vel", 100.0))
        letters = ["X", "Y", "Z"]
        if axis < len(letters) and serial_bus:
            feed = abs(vel) * 60.0
            dist = 10000.0 * (1 if vel >= 0 else -1)
            jog_cmd = f"$J=G91 {letters[axis]}{dist:.1f} F{feed:.1f}"
            serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd == "jog_cont_multi":
        axes = msg.get("axes", [])
        letters = ["X", "Y", "Z"]
        if axes and serial_bus:
            parts = []
            max_feed = 0.0
            for a in axes:
                idx = a.get("axis", 0)
                vel = float(a.get("vel", 0.0))
                if idx < len(letters):
                    dist = 10000.0 * (1 if vel >= 0 else -1)
                    parts.append(f"{letters[idx]}{dist:.1f}")
                    max_feed = max(max_feed, abs(vel) * 60.0)
            if parts:
                jog_cmd = f"$J=G91 {' '.join(parts)} F{max_feed:.1f}"
                serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd in ("jog_stop", "jog_stop_multi"):
        if serial_bus:
            serial_bus.send_rt(b"\x85")  # Jog Cancel
        return {"type": "reply", "ok": True}

    if cmd == "jog_incr":
        axis     = msg.get("axis", 0)
        vel      = float(msg.get("vel", 100.0))
        distance = float(msg.get("distance", 1.0))
        letters  = ["X", "Y", "Z"]
        if axis < len(letters) and serial_bus:
            feed = abs(vel) * 60.0          # mm/s → mm/min
            # distance はUIから方向込み（±mm）で来るのでそのまま使う
            jog_cmd = f"$J=G91 {letters[axis]}{distance:.4f} F{feed:.1f}"
            serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd == "jog_incr_multi":
        axes    = msg.get("axes", [])
        letters = ["X", "Y", "Z"]
        if axes and serial_bus:
            parts    = []
            max_feed = 0.0
            for a in axes:
                idx  = a.get("axis", 0)
                vel  = float(a.get("vel", 0.0))
                dist = float(a.get("distance", 0.0))
                if idx < len(letters):
                    parts.append(f"{letters[idx]}{dist:.4f}")
                    max_feed = max(max_feed, abs(vel) * 60.0)
            if parts:
                jog_cmd = f"$J=G91 {' '.join(parts)} F{max_feed:.1f}"
                serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    # ── オーバーライド ────────────────────────────────────────────
    if cmd == "set_feed_override":
        scale = float(msg.get("scale", 1.0))
        with machine.lock:
            machine.feed_override = scale
        # grblHAL フィードオーバーライド: % 単位の実時間コマンド
        # 0x90=100%, 0x91=+10%, 0x92=-10%, 0x93=+1%, 0x94=-1%
        # ここでは近似として何もしない（将来拡張）
        return {"type": "reply", "ok": True}

    # ── ファイルロード ────────────────────────────────────────────
    if cmd == "load_file":
        path = msg.get("path", "")
        if not os.path.exists(path):
            return {"type": "reply", "ok": False, "error": f"File not found: {path}"}
        with machine.lock:
            machine.active_file = path
        # ツールパスを非同期で抽出して viewer_gcode を送信
        asyncio.create_task(_send_viewer_gcode(path))
        return {"type": "reply", "ok": True}

    if cmd == "unload_file":
        with machine.lock:
            machine.active_file = None
        return {"type": "reply", "ok": True}

    # ── ホーミング（grblHAL ホーミング無効なので no-op）────────────
    if cmd in ("home", "home_all", "unhome_all"):
        # ホーミング無効（$22=0）のためno-op
        # homed=Trueは固定なので状態変化なし
        return {"type": "reply", "ok": True}

    # ── スピンドル ───────────────────────────────────────────────
    if cmd == "spindle_forward":
        speed = float(msg.get("speed", 1000))
        if serial_bus:
            await loop.run_in_executor(None, serial_bus.send_cmd, f"M3 S{speed:.0f}")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_reverse":
        speed = float(msg.get("speed", 1000))
        if serial_bus:
            await loop.run_in_executor(None, serial_bus.send_cmd, f"M4 S{speed:.0f}")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_stop":
        if serial_bus:
            await loop.run_in_executor(None, serial_bus.send_cmd, "M5")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_increase":
        if serial_bus:
            serial_bus.send_rt(b"\x9A")  # spindle speed +10%
        return {"type": "reply", "ok": True}

    if cmd == "spindle_decrease":
        if serial_bus:
            serial_bus.send_rt(b"\x9B")  # spindle speed -10%
        return {"type": "reply", "ok": True}

    if cmd == "save_settings":
        section = msg.get("section", "")
        data    = msg.get("data")
        loop    = asyncio.get_event_loop()
        def _save():
            settings = _load_settings()
            settings[section] = data
            _save_settings(settings)
        await loop.run_in_executor(None, _save)
        return {"type": "reply", "ok": True}

    if cmd == "timing_log":
        return {"type": "reply", "ok": True}

    if cmd == "get_tool_table":
        return {"type": "reply", "ok": True, "tool_table": []}

    if cmd in ("tool_change", "save_tool", "add_tool", "delete_tool",
               "set_optional_stop", "set_block_delete",
               "set_mode", "home_all", "unhome_all"):
        return {"type": "reply", "ok": True}

    log.debug(f"Unhandled cmd: {cmd}")
    return {"type": "reply", "ok": False, "error": f"Unknown command: {cmd}"}


async def _send_viewer_gcode(ngc_path: str):
    """viewer_gcode メッセージを全クライアントに送信（非同期）"""
    if not RS274_AVAILABLE:
        return
    loop = asyncio.get_event_loop()
    try:
        feed, rapid = await loop.run_in_executor(None, extract_toolpath, ngc_path)
        await clients.broadcast({
            "type": "viewer_gcode",
            "data": {
                "file":  ngc_path,
                "feed":  feed,
                "rapid": rapid,
            },
        })
        log.info(f"viewer_gcode: {len(feed)} feed, {len(rapid)} rapid pts")
    except Exception as e:
        log.warning(f"viewer_gcode error: {e}")


# ─────────────────────────────────────────────────────────────────────
# FastAPI アプリ
# ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global serial_bus
    NGC_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # GPIO70（レベルシフタ Enable）をgpiod 2.x経由で保持
    # arduino-router停止後にLowになるため、gateway自身が保持する責任を持つ
    _gpio_request = None
    try:
        import gpiod
        _gpio_request = gpiod.request_lines(
            '/dev/gpiochip1',
            consumer='grbl_gateway',
            config={70: gpiod.LineSettings(
                direction=gpiod.line.Direction.OUTPUT,
                output_value=gpiod.line.Value.ACTIVE
            )}
        )
        time.sleep(0.05)
        log.info("GPIO70 set HIGH via gpiod (level shifter enabled)")
    except Exception as e:
        log.warning(f"GPIO70 gpiod setup failed: {e}")

    # シリアル接続
    port = os.environ.get("GRBL_PORT", DEFAULT_PORT)
    dry_run = os.environ.get("GRBL_DRY_RUN") == "1"
    if dry_run:
        log.info("Dry-run mode: serial disabled")
        serial_bus = None
    else:
        try:
            serial_bus = SerialBus(port, DEFAULT_BAUD)
            # 初期化: アラームクリア + ワーク原点設定
            serial_bus.send_cmd("$X")
            time.sleep(0.2)
            serial_bus.send_cmd("G92 X0 Y0 Z0")
            log.info("grblHAL initialized")
        except Exception as e:
            log.warning(f"Serial unavailable ({e}), running in dry-run mode")
            serial_bus = None

    # viewer_init（最小限のマシン定義）
    viewer_init_msg = {
        "type": "viewer_init",
        "data": {
            "units":  "mm",
            "axes":   ["X", "Y", "Z"],
            "stl_base_url": "",
            "machine_bounds": {
                "origin": [0, 0, 0],
                "size":   [200, 200, 200],
            },
            "groups":     [],
            "parts":      [],
            "kinematics": [],
            "workGroup":  None,
            "toolGroup":  None,
        },
    }

    # status ループ起動
    loop_task = asyncio.create_task(status_loop())

    yield  # アプリ実行

    loop_task.cancel()
    if serial_bus:
        serial_bus.close()
    if _gpio_request:
        try:
            _gpio_request.release()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)

# ── WebSocket エンドポイント ──────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    await clients.add(ws)
    log.info(f"WS connected: {ws.client}")

    # 接続直後に viewer_init を送信
    await ws.send_text(json.dumps({
        "type": "viewer_init",
        "data": {
            "units": "mm",
            "axes": ["X", "Y", "Z"],
            "stl_base_url": "",
            "machine_bounds": {"origin": [0, 0, 0], "size": [200, 200, 200]},
            "groups": [], "parts": [], "kinematics": [],
            "workGroup": None, "toolGroup": None,
        },
    }))

    # ロード済みファイルがあれば viewer_gcode を送信
    with machine.lock:
        ngc = machine.active_file
    if ngc:
        asyncio.create_task(_send_viewer_gcode(ngc))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            reply = await handle_command(ws, msg)
            await ws.send_text(json.dumps(reply))
    except WebSocketDisconnect:
        log.info(f"WS disconnected: {ws.client}")
    finally:
        await clients.remove(ws)


SETTINGS_FILE = Path("/home/arduino/.config/lcnc_gateway/settings.json")

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


@app.get("/settings")
async def get_settings():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _load_settings)
    return {"ok": True, "settings": data}


@app.put("/settings/{section}")
@app.post("/settings/{section}")
async def put_settings_section(section: str, request: Request):
    body = await request.json()
    loop = asyncio.get_event_loop()
    def _save():
        data = _load_settings()
        data[section] = body.get("data", body)
        _save_settings(data)
    await loop.run_in_executor(None, _save)
    return {"ok": True}


@app.delete("/settings")
async def delete_settings():
    SETTINGS_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/gcode")
async def get_gcode_content(path: str):
    """NGCファイルの内容をテキストで返す（GcodePanel表示用）"""
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=file_path.read_text(errors="replace"))


@app.put("/save")
async def save_file(request: Request):
    """GcodePanelのEditで編集したファイルを保存"""
    body = await request.json()
    path    = body.get("path", "")
    content = body.get("content", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    file_path = Path(path)
    # セキュリティ: アップロードディレクトリ配下のみ許可
    try:
        file_path.resolve().relative_to(NGC_UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside upload directory")
    file_path.write_text(content)
    log.info(f"Saved: {file_path} ({len(content)} bytes)")
    # lcncWs._applyGcodeFileは同一パスを無視するため、
    # file=nullを先送りして強制リセットしてからviewer_gcodeを再送する
    await clients.broadcast({"type": "viewer_gcode", "data": {"file": None}})
    asyncio.create_task(_send_viewer_gcode(str(file_path)))
    return {"ok": True, "path": str(file_path), "size": len(content)}


# ── NGC ファイルアップロード ──────────────────────────────────────────
@app.post("/upload")
async def upload_ngc(file: UploadFile = File(...)):
    """
    NGC ファイルをアップロードして active_file に設定。
    viewer_gcode を非同期で生成して全クライアントへ送信。
    """
    filename = Path(file.filename).name
    dest = NGC_UPLOAD_DIR / filename
    content = await file.read()
    dest.write_bytes(content)

    with machine.lock:
        machine.active_file = str(dest)

    asyncio.create_task(_send_viewer_gcode(str(dest)))
    log.info(f"Uploaded: {dest} ({len(content)} bytes)")
    return {"ok": True, "path": str(dest), "filename": filename}


@app.get("/files")
async def list_files(subdir: str = ""):
    """アップロード済みNGCファイル一覧（lcnc-suite FilesResponse形式）"""
    base = NGC_UPLOAD_DIR / subdir if subdir else NGC_UPLOAD_DIR
    entries = []
    if base.exists():
        for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if p.is_dir():
                entries.append({
                    "name": p.name,
                    "type": "directory",
                    "path": str(p),
                })
            elif p.suffix.lower() in (".ngc", ".nc", ".gcode", ".tap", ".txt"):
                entries.append({
                    "name": p.name,
                    "type": "file",
                    "path": str(p),
                    "size": p.stat().st_size,
                    "modified": int(p.stat().st_mtime),
                })
    return {
        "ok":      True,
        "nc_dir":  str(NGC_UPLOAD_DIR),
        "subdir":  subdir,
        "entries": entries,
    }


@app.post("/telemetry")
async def telemetry(request: Request):
    """lcnc-webui からのテレメトリを受け取る（ログのみ）"""
    return JSONResponse({"ok": True})



# ── grblHAL settings UI ────────────────────────────────────────
import pathlib as _pl2

@app.get("/grbl-settings")
async def _grbl_settings_page():
    from fastapi.responses import HTMLResponse as _HR
    return _HR((_pl2.Path(__file__).parent / "grbl-settings.html").read_text())

@app.get("/grbl-settings-data")
async def _grbl_settings_get():
    if not serial_bus:
        return JSONResponse({"ok": False, "error": "no serial"})
    import re as _re
    loop = asyncio.get_event_loop()
    try:
        lines = await loop.run_in_executor(None, serial_bus.send_and_collect, "$$")
        settings = []
        for ln in lines:
            m = _re.match(r'\$(\d+)=([^\s(]+)(?:\s+\(([^)]*)\))?', ln)
            if m:
                settings.append({"id": int(m.group(1)), "value": m.group(2), "desc": m.group(3) or ""})
        return JSONResponse({"ok": True, "settings": settings})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.post("/grbl-settings-data")
async def _grbl_settings_set(request: Request):
    body = await request.json()
    n, v = body.get("n"), body.get("v")
    if n is None or v is None:
        return JSONResponse({"ok": False, "error": "missing n or v"})
    if not serial_bus:
        return JSONResponse({"ok": False, "error": "no serial"})
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, serial_bus.send_cmd, f"${n}={v}")
        return JSONResponse({"ok": result == "ok", "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

# ── G-code popup editor ──────────────────────────────────────────
import pathlib as _pl
from fastapi.responses import HTMLResponse as _HtmlR

_EDITOR_DIR = _pl.Path(__file__).parent

@app.get("/active-file")
async def _active_file():
    with machine.lock:
        f = machine.active_file
    return JSONResponse({"path": f or ""})

@app.get("/read-file")
async def _read_file(path: str):
    from fastapi.responses import PlainTextResponse
    p = _pl.Path(path)
    if not p.exists() or p.suffix.lower() not in {".ngc",".nc",".gcode",".tap",".txt"}:
        return JSONResponse({"error":"not found"}, status_code=404)
    return PlainTextResponse(p.read_text(errors="replace"))

@app.get("/editor")
async def _editor():
    return _HtmlR((_EDITOR_DIR / "editor.html").read_text())

@app.get("/editor-widget.js")
async def _editor_widget():
    from fastapi.responses import Response
    return Response((_EDITOR_DIR / "editor-widget.js").read_text(), media_type="application/javascript")

# ── 静的ファイル（lcnc-webui dist/）────────────────────────────────
if WEBUI_DIST.exists():
    app.mount("/", StaticFiles(directory=str(WEBUI_DIST), html=True), name="webui")
    log.info(f"Serving WebUI from {WEBUI_DIST}")
else:
    @app.get("/")
    async def root():
        return JSONResponse({
            "status": "gateway running",
            "webui":  "not found — build lcnc-webui and place dist/ next to this file",
        })
    log.warning(f"WebUI dist not found at {WEBUI_DIST}")


# ─────────────────────────────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="grbl-lcnc WebSocket gateway")
    parser.add_argument("--port",     default=DEFAULT_PORT, help="Serial port")
    parser.add_argument("--web-port", default=8000, type=int, help="HTTP listen port")
    parser.add_argument("--host",     default="0.0.0.0")
    parser.add_argument("--dry-run",  action="store_true", help="シリアル未接続で起動（テスト用）")
    args = parser.parse_args()

    os.environ["GRBL_PORT"] = args.port
    if args.dry_run:
        os.environ["GRBL_DRY_RUN"] = "1"
    uvicorn.run(app, host=args.host, port=args.web_port, log_level="info")
