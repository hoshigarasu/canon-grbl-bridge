"""rs274ngc bridge（GrblBridge）— canon コール → grblHAL コマンド変換。"""
import os
import shutil
import tempfile
import threading
from typing import Optional

from gateway.config import log, MM_PER_INCH, INITCODE
from gateway import runtime as rt

# rs274ngc (linuxcnc-uspace パッケージ)
try:
    import gcode
    from rs274.interpret import Translated, StatMixin
    RS274_AVAILABLE = True
except ImportError:
    RS274_AVAILABLE = False
    gcode = None  # type: ignore[assignment]

    class Translated:  # type: ignore[no-redef]
        pass

    class StatMixin:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

    log.warning("rs274ngc not available — toolpath extraction disabled")


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
        result = rt.serial_bus.send_cmd(cmd)
        if result != "ok":
            # _wait_ok が既に notify_error で原因を通知している。
            # ここで raise することで、ALARM 状態の grblHAL に後続コマンドを
            # 投げ続ける（全部 error:9 で弾かれる）状態を防ぐ。
            raise RuntimeError(f"grblHAL rejected: {result} (sent: {cmd})")

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
        self._send(f"G4 P{seconds:.3f}")

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
