"""ツールパス抽出（viewer_gcode 用）+ 加工時間推定。"""
import os
import shutil
import tempfile
import threading

from gateway.config import log, INITCODE
from gateway.canon import GrblBridge, gcode


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


def _estimate_time(commands: list, rapid_mm_min: float = 3000.0) -> float:
    """コマンドリストから推定加工時間（秒）を計算する。"""
    import math, re
    pos  = [0.0, 0.0, 0.0]
    feed = 1000.0
    total = 0.0
    tok = re.compile(r'([XYZIJFP])([-\d.]+)', re.I)

    for cmd in commands:
        if not cmd:
            continue
        t = {m.group(1).upper(): float(m.group(2)) for m in tok.finditer(cmd)}
        cu = cmd.upper()
        if 'F' in t:
            feed = t['F']

        if re.search(r'\bG0?0\b', cu):           # G0 / G00 rapid
            tgt = [t.get('X', pos[0]), t.get('Y', pos[1]), t.get('Z', pos[2])]
            d = math.sqrt(sum((tgt[i]-pos[i])**2 for i in range(3)))
            if d > 0 and rapid_mm_min > 0:
                total += d / rapid_mm_min * 60
            pos = tgt

        elif re.search(r'\bG0?1\b', cu):          # G1 feed
            tgt = [t.get('X', pos[0]), t.get('Y', pos[1]), t.get('Z', pos[2])]
            d = math.sqrt(sum((tgt[i]-pos[i])**2 for i in range(3)))
            if d > 0 and feed > 0:
                total += d / feed * 60
            pos = tgt

        elif re.search(r'\bG0?[23]\b', cu):       # G2/G3 arc
            cw  = bool(re.search(r'\bG0?2\b', cu))
            tx  = t.get('X', pos[0]); ty = t.get('Y', pos[1]); tz = t.get('Z', pos[2])
            ci  = t.get('I', 0.0);   cj = t.get('J', 0.0)
            cx  = pos[0] + ci;       cy = pos[1] + cj
            r   = math.hypot(ci, cj)
            if r > 1e-9:
                a0 = math.atan2(pos[1] - cy, pos[0] - cx)
                a1 = math.atan2(ty - cy,     tx - cx)
                if cw:
                    if a1 >= a0: a1 -= 2 * math.pi
                else:
                    if a1 <= a0: a1 += 2 * math.pi
                arc_len = r * abs(a1 - a0)
                dz  = abs(tz - pos[2])
                dist = math.sqrt(arc_len**2 + dz**2)
                if feed > 0:
                    total += dist / feed * 60
            pos = [tx, ty, tz]

        elif re.search(r'\bG0?4\b', cu):          # G4 dwell (ms in grblHAL)
            total += t.get('P', 0) / 1000.0

    return total
