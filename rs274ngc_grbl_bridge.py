#!/usr/bin/env python3
"""
rs274ngc_grbl_bridge.py
rs274ngc の canon コールを grblHAL コマンドに変換して ttyHS1 へ送信する。

Usage:
    python3 rs274ngc_grbl_bridge.py <gcode_file> [--dry-run] [--port /dev/ttyHS1]

Architecture:
    G-code file
        ↓ gcode.parse()
    GrblBridge (canon コール受信)
        ↓ straight_feed / arc_feed / ...
    serial.Serial → /dev/ttyHS1 → grblHAL (STM32U585)
"""

import sys
import os
import tempfile
import shutil
import argparse
import serial
import gcode
from rs274.interpret import Translated, StatMixin

# ------------------------------------------------------------------ #
# 定数
# ------------------------------------------------------------------ #
DEFAULT_PORT   = "/dev/ttyHS1"
DEFAULT_BAUD   = 115200
SERIAL_TIMEOUT = 10          # 秒（長い移動への応答待ち）
INITCODE       = "G17 G40 G49 G80 G90"   # 標準初期化モーダル

# rs274ngc の canon コールは常にインチ（内部単位）で渡される。
# G-code ファイルが G21(mm) であっても変わらない。
# このスケールファクタで全座標・送り速度を mm / mm/min に変換する。
MM_PER_INCH = 25.4


# ------------------------------------------------------------------ #
# FakeStat: linuxcnc.stat() の代替（HAL 未起動時用）
# ------------------------------------------------------------------ #
class FakeStat:
    """
    LinuxCNC HAL が動いていない環境用の stat 代替。
    StatMixin が参照するフィールドのみ定義。
    """
    tool_table    = ()       # ツールなし（T0のみ）
    angular_units = 1.0      # degrees
    linear_units  = 1.0      # 1.0 = inch (LinuxCNC stat convention); coordinates scaled by MM_PER_INCH in bridge
    axis_mask     = 0b111    # XYZ = 7
    block_delete  = False


# ------------------------------------------------------------------ #
# GrblBridge: canon → grblHAL 変換本体
# ------------------------------------------------------------------ #
class GrblBridge(Translated, StatMixin):
    """
    Translated  : G5x/G92 オフセット・回転を適用して
                  straight_feed_translated / straight_traverse_translated を呼ぶ
    StatMixin   : ツールテーブル・単位系を FakeStat から取得

    arc_feed は Translated を経由しない（直接オーバーライド）。
    ArcsToSegmentsMixin は使わず G2/G3 を grblHAL に直接送信する。
    """

    def __init__(self, stat, ser=None):
        """
        stat : FakeStat または linuxcnc.stat() オブジェクト
        ser  : serial.Serial インスタンス（None = dry-run モード）
        """
        StatMixin.__init__(self, stat, stat.random_toolchanger
                           if hasattr(stat, 'random_toolchanger') else 0)
        self.serial    = ser
        self.feed_rate = 0.0
        self.current_x = 0.0
        self.current_y = 0.0
        self.current_z = 0.0
        # gcode.parse が参照する必須属性
        self.parameter_file = ""

    # ---------------------------------------------------------------- #
    # 内部ユーティリティ
    # ---------------------------------------------------------------- #

    def _send(self, cmd: str):
        """
        1行送信して grblHAL の 'ok' を待つ。
        serial=None の場合は dry-run（標準出力に表示）。
        [MSG:...] 等の非同期メッセージは読み飛ばして ok/error まで待つ。
        """
        line = cmd.strip()
        if self.serial is None:
            print(f"[DRY] {line}")
            return
        self.serial.write((line + "\n").encode())
        while True:
            resp = self.serial.readline().decode(errors="replace").strip()
            if not resp:
                continue                          # タイムアウト空行、再待機
            if resp.startswith("["):
                print(f"[grbl] {resp}", file=sys.stderr)
                continue
            if resp.startswith("ALARM"):
                msg = f"grblHAL ALARM: {resp!r}  (sent: {line!r})"
                print(f"[FATAL] {msg}", file=sys.stderr)
                raise RuntimeError(msg)
            if resp.startswith("error"):
                msg = f"grblHAL error: {resp!r}  (sent: {line!r})"
                print(f"[FATAL] {msg}", file=sys.stderr)
                raise RuntimeError(msg)
            if resp == "ok":
                return
            print(f"[recv] {resp!r}", file=sys.stderr)

    def _update_pos(self, x, y, z):
        self.current_x, self.current_y, self.current_z = x, y, z

    def get_external_length_units(self):
        """rs274ngc の内部単位（inch）を mm で出力させる。
        1 inch = 25.4 mm → 25.4 を返すことで座標・送り速度が mm / mm/min になる。"""
        return 25.4

    # ---------------------------------------------------------------- #
    # Translated が要求するメソッド
    # G5x/G92 オフセット・rotation_xy 適用済みの座標で呼ばれる
    # ---------------------------------------------------------------- #

    def straight_traverse_translated(self, x, y, z, a, b, c, u, v, w):
        """G0 ラピッド移動（inch → mm 変換）"""
        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH
        self._send(f"G0 X{x:.4f} Y{y:.4f} Z{z:.4f}")
        self._update_pos(x, y, z)

    def straight_feed_translated(self, x, y, z, a, b, c, u, v, w):
        """G1 直線切削送り（inch → mm 変換）"""
        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH
        self._send(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F{self.feed_rate:.2f}")
        self._update_pos(x, y, z)

    # ---------------------------------------------------------------- #
    # 円弧（Translated を通らないため直接オーバーライド）
    # ArcsToSegmentsMixin は使わず G2/G3 を直接送信
    # ---------------------------------------------------------------- #

    def arc_feed(self, x1, y1, cx, cy, rot, z1, a, b, c, u, v, w):
        """
        G2/G3 円弧送り（inch → mm 変換）。
        cx, cy : 円弧中心（絶対座標、インチ）
        rot    : +1 = CCW (G3),  -1 = CW (G2)
        grblHAL の I,J は現在位置からのオフセット（mm）。
        """
        x1 *= MM_PER_INCH; y1 *= MM_PER_INCH; z1 *= MM_PER_INCH
        cx *= MM_PER_INCH; cy *= MM_PER_INCH
        i = cx - self.current_x
        j = cy - self.current_y
        g = "G3" if rot > 0 else "G2"
        self._send(
            f"{g} X{x1:.4f} Y{y1:.4f} Z{z1:.4f} "
            f"I{i:.4f} J{j:.4f} F{self.feed_rate:.2f}"
        )
        self._update_pos(x1, y1, z1)

    # ---------------------------------------------------------------- #
    # その他 canon コール
    # ---------------------------------------------------------------- #

    def set_feed_rate(self, rate):
        """送り速度を保持（inch/min → mm/min 変換）"""
        self.feed_rate = rate * MM_PER_INCH

    def dwell(self, seconds):
        """G4 Pms ドウェル"""
        ms = int(seconds * 1000)
        self._send(f"G4 P{ms}")

    def spindle_on(self, speed, *args):
        """M3 スピンドル正転"""
        self._send(f"M3 S{speed:.0f}")

    def spindle_off(self):
        """M5 スピンドル停止"""
        self._send("M5")

    def mist_on(self):
        self._send("M7")

    def flood_on(self):
        self._send("M8")

    def mist_off(self):
        self._send("M9")

    def flood_off(self):
        self._send("M9")

    def set_plane(self, plane):
        """
        G17/G18/G19 作業平面設定。
        plane: 1=XY(G17), 2=XZ(G18), 3=YZ(G19)
        """
        plane_codes = {1: "G17", 2: "G18", 3: "G19"}
        code = plane_codes.get(plane, "G17")
        self._send(code)
        self._plane = plane

    # ---------------------------------------------------------------- #
    # grblHAL へ渡せるその他 modal 設定
    # ---------------------------------------------------------------- #

    def set_feed_mode(self, mode):
        """G93/G94/G95 送りモード。mode=0→G94(units/min), 1→G93(逆時間)"""
        if mode == 0:
            self._send("G94")
        elif mode == 1:
            self._send("G93")

    def set_distance_mode(self, mode):
        """G90/G91 距離モード。mode=0→G90(絶対), 1→G91(相対)"""
        self._send("G90" if mode == 0 else "G91")

    def set_motion_control_mode(self, mode, tolerance):
        """G61/G64 モーション制御。grblHAL は G64 相当が基本なので無視。"""
        pass

    def comment(self, msg):
        """コメントは grblHAL に送らない（バッファ節約）"""
        pass

    def next_line(self, state):
        """行番号・モーダル状態を保持（エラー報告用）"""
        self.state = state

    def program_end(self):
        """M2 プログラム終了"""
        self._send("M2")

    # ---------------------------------------------------------------- #
    # 未実装 canon メソッドのフォールバック
    # 呼ばれたメソッド名を警告表示して無視する（クラッシュ防止）
    # ---------------------------------------------------------------- #

    def check_abort(self):
        """rs274ngc が弧実行中に定期呼び出しする中断チェック。False = 継続。"""
        return False

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        print(f"[WARN] unimplemented canon: {name}()", file=sys.stderr)
        return lambda *args, **kwargs: None

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()


# ------------------------------------------------------------------ #
# エントリポイント
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(
        description="rs274ngc G-code → grblHAL (ttyHS1) bridge"
    )
    parser.add_argument("gcode_file", help="実行する G-code ファイル")
    parser.add_argument("--port",     default=DEFAULT_PORT,
                        help=f"シリアルポート (default: {DEFAULT_PORT})")
    parser.add_argument("--baud",     default=DEFAULT_BAUD, type=int,
                        help=f"ボーレート (default: {DEFAULT_BAUD})")
    parser.add_argument("--dry-run",  action="store_true",
                        help="シリアル送信せず、生成コマンドを標準出力に表示")
    args = parser.parse_args()

    if not os.path.exists(args.gcode_file):
        print(f"Error: file not found: {args.gcode_file}", file=sys.stderr)
        sys.exit(1)

    # シリアル接続（dry-run では None）
    ser = None
    if not args.dry_run:
        ser = serial.Serial(args.port, args.baud, timeout=SERIAL_TIMEOUT)
        import time
        time.sleep(0.5)
        ser.reset_input_buffer()
        # アラーム状態を解除（前回エラー時に ALARM に入っていた場合の対策）
        ser.write(b"$X\n")
        time.sleep(0.3)
        ser.reset_input_buffer()
        # 現在位置をワーク原点にリセット（bridge の (0,0,0) と grblHAL を一致させる）
        ser.write(b"G92 X0 Y0 Z0\n")
        time.sleep(0.3)
        ser.reset_input_buffer()

    # parameter ファイル（G-code 変数の永続化）
    td = tempfile.mkdtemp()
    try:
        temp_var = os.path.join(td, "bridge.var")
        # デフォルト .var が存在すれば引き継ぐ（なければ空ファイル）
        default_var = "/usr/share/linuxcnc/ncfiles/linuxcnc.var"
        if os.path.exists(default_var):
            shutil.copy(default_var, temp_var)
        else:
            open(temp_var, "w").close()

        stat    = FakeStat()
        bridge  = GrblBridge(stat, ser)
        bridge.parameter_file = temp_var

        # G-code input is always mm (G21).
        # Canon output units are controlled by get_external_length_units() → 25.4 → mm.
        unitcode = "G21"
        initcode = INITCODE

        result, seq = gcode.parse(args.gcode_file, bridge, unitcode, initcode)
        if result > gcode.MIN_ERROR:
            print(f"G-code error at line {seq}: error code {result}",
                  file=sys.stderr)
            sys.exit(1)

    finally:
        bridge.close()
        shutil.rmtree(td)


if __name__ == "__main__":
    main()
