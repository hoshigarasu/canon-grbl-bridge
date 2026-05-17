warning: in the working copy of 'rs274ngc_grbl_bridge.py', LF will be replaced by CRLF the next time Git touches it
[1mdiff --git a/rs274ngc_grbl_bridge.py b/rs274ngc_grbl_bridge.py[m
[1mindex 20bd15d..0ab3401 100644[m
[1m--- a/rs274ngc_grbl_bridge.py[m
[1m+++ b/rs274ngc_grbl_bridge.py[m
[36m@@ -29,9 +29,13 @@[m [mfrom rs274.interpret import Translated, StatMixin[m
 DEFAULT_PORT   = "/dev/ttyHS1"[m
 DEFAULT_BAUD   = 115200[m
 SERIAL_TIMEOUT = 10          # 秒（長い移動への応答待ち）[m
[31m-UNIT_MM        = 1.0         # linear_units の mm 表現[m
 INITCODE       = "G17 G40 G49 G80 G90"   # 標準初期化モーダル[m
 [m
[32m+[m[32m# rs274ngc の canon コールは常にインチ（内部単位）で渡される。[m
[32m+[m[32m# G-code ファイルが G21(mm) であっても変わらない。[m
[32m+[m[32m# このスケールファクタで全座標・送り速度を mm / mm/min に変換する。[m
[32m+[m[32mMM_PER_INCH = 25.4[m
[32m+[m
 [m
 # ------------------------------------------------------------------ #[m
 # FakeStat: linuxcnc.stat() の代替（HAL 未起動時用）[m
[36m@@ -43,7 +47,7 @@[m [mclass FakeStat:[m
     """[m
     tool_table    = ()       # ツールなし（T0のみ）[m
     angular_units = 1.0      # degrees[m
[31m-    linear_units  = UNIT_MM  # mm[m
[32m+[m[32m    linear_units  = 1.0      # 1.0 = inch (LinuxCNC stat convention); coordinates scaled by MM_PER_INCH in bridge[m
     axis_mask     = 0b111    # XYZ = 7[m
     block_delete  = False[m
 [m
[36m@@ -98,27 +102,40 @@[m [mclass GrblBridge(Translated, StatMixin):[m
             if resp.startswith("["):[m
                 print(f"[grbl] {resp}", file=sys.stderr)[m
                 continue[m
[32m+[m[32m            if resp.startswith("ALARM"):[m
[32m+[m[32m                msg = f"grblHAL ALARM: {resp!r}  (sent: {line!r})"[m
[32m+[m[32m                print(f"[FATAL] {msg}", file=sys.stderr)[m
[32m+[m[32m                raise RuntimeError(msg)[m
             if resp.startswith("error"):[m
[31m-                raise RuntimeError(f"grblHAL error: {resp!r}  (sent: {line!r})")[m
[32m+[m[32m                msg = f"grblHAL error: {resp!r}  (sent: {line!r})"[m
[32m+[m[32m                print(f"[FATAL] {msg}", file=sys.stderr)[m
[32m+[m[32m                raise RuntimeError(msg)[m
             if resp == "ok":[m
                 return[m
[31m-            print(f"[warn] {resp!r}", file=sys.stderr)[m
[32m+[m[32m            print(f"[recv] {resp!r}", file=sys.stderr)[m
 [m
     def _update_pos(self, x, y, z):[m
         self.current_x, self.current_y, self.current_z = x, y, z[m
 [m
[32m+[m[32m    def get_external_length_units(self):[m
[32m+[m[32m        """rs274ngc の内部単位（inch）を mm で出力させる。[m
[32m+[m[32m        1 inch = 25.4 mm → 25.4 を返すことで座標・送り速度が mm / mm/min になる。"""[m
[32m+[m[32m        return 25.4[m
[32m+[m
     # ---------------------------------------------------------------- #[m
     # Translated が要求するメソッド[m
     # G5x/G92 オフセット・rotation_xy 適用済みの座標で呼ばれる[m
     # ---------------------------------------------------------------- #[m
 [m
     def straight_traverse_translated(self, x, y, z, a, b, c, u, v, w):[m
[31m-        """G0 ラピッド移動"""[m
[32m+[m[32m        """G0 ラピッド移動（inch → mm 変換）"""[m
[32m+[m[32m        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH[m
         self._send(f"G0 X{x:.4f} Y{y:.4f} Z{z:.4f}")[m
         self._update_pos(x, y, z)[m
 [m
     def straight_feed_translated(self, x, y, z, a, b, c, u, v, w):[m
[31m-        """G1 直線切削送り"""[m
[32m+[m[32m        """G1 直線切削送り（inch → mm 変換）"""[m
[32m+[m[32m        x *= MM_PER_INCH; y *= MM_PER_INCH; z *= MM_PER_INCH[m
         self._send(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F{self.feed_rate:.2f}")[m
         self._update_pos(x, y, z)[m
 [m
[36m@@ -129,11 +146,13 @@[m [mclass GrblBridge(Translated, StatMixin):[m
 [m
     def arc_feed(self, x1, y1, cx, cy, rot, z1, a, b, c, u, v, w):[m
         """[m
[31m-        G2/G3 円弧送り。[m
[31m-        cx, cy : 円弧中心（絶対座標）[m
[32m+[m[32m        G2/G3 円弧送り（inch → mm 変換）。[m
[32m+[m[32m        cx, cy : 円弧中心（絶対座標、インチ）[m
         rot    : +1 = CCW (G3),  -1 = CW (G2)[m
[31m-        grblHAL の I,J は現在位置からのオフセット。[m
[32m+[m[32m        grblHAL の I,J は現在位置からのオフセット（mm）。[m
         """[m
[32m+[m[32m        x1 *= MM_PER_INCH; y1 *= MM_PER_INCH; z1 *= MM_PER_INCH[m
[32m+[m[32m        cx *= MM_PER_INCH; cy *= MM_PER_INCH[m
         i = cx - self.current_x[m
         j = cy - self.current_y[m
         g = "G3" if rot > 0 else "G2"[m
[36m@@ -148,8 +167,8 @@[m [mclass GrblBridge(Translated, StatMixin):[m
     # ---------------------------------------------------------------- #[m
 [m
     def set_feed_rate(self, rate):[m
[31m-        """送り速度を保持（次の G1/G2/G3 に埋め込む）"""[m
[31m-        self.feed_rate = rate[m
[32m+[m[32m        """送り速度を保持（inch/min → mm/min 変換）"""[m
[32m+[m[32m        self.feed_rate = rate * MM_PER_INCH[m
 [m
     def dwell(self, seconds):[m
         """G4 Pms ドウェル"""[m
[36m@@ -222,6 +241,10 @@[m [mclass GrblBridge(Translated, StatMixin):[m
     # 呼ばれたメソッド名を警告表示して無視する（クラッシュ防止）[m
     # ---------------------------------------------------------------- #[m
 [m
[32m+[m[32m    def check_abort(self):[m
[32m+[m[32m        """rs274ngc が弧実行中に定期呼び出しする中断チェック。False = 継続。"""[m
[32m+[m[32m        return False[m
[32m+[m
     def __getattr__(self, name):[m
         if name.startswith("_"):[m
             raise AttributeError(name)[m
[36m@@ -258,10 +281,17 @@[m [mdef main():[m
     ser = None[m
     if not args.dry_run:[m
         ser = serial.Serial(args.port, args.baud, timeout=SERIAL_TIMEOUT)[m
[31m-        # grblHAL 起動メッセージを読み捨て[m
         import time[m
         time.sleep(0.5)[m
         ser.reset_input_buffer()[m
[32m+[m[32m        # アラーム状態を解除（前回エラー時に ALARM に入っていた場合の対策）[m
[32m+[m[32m        ser.write(b"$X\n")[m
[32m+[m[32m        time.sleep(0.3)[m
[32m+[m[32m        ser.reset_input_buffer()[m
[32m+[m[32m        # 現在位置をワーク原点にリセット（bridge の (0,0,0) と grblHAL を一致させる）[m
[32m+[m[32m        ser.write(b"G92 X0 Y0 Z0\n")[m
[32m+[m[32m        time.sleep(0.3)[m
[32m+[m[32m        ser.reset_input_buffer()[m
 [m
     # parameter ファイル（G-code 変数の永続化）[m
     td = tempfile.mkdtemp()[m
[36m@@ -278,8 +308,9 @@[m [mdef main():[m
         bridge  = GrblBridge(stat, ser)[m
         bridge.parameter_file = temp_var[m
 [m
[31m-        # linear_units==1 → mm(G21), それ以外 → inch(G20)[m
[31m-        unitcode = "G%d" % (20 + (stat.linear_units == UNIT_MM))[m
[32m+[m[32m        # G-code input is always mm (G21).[m
[32m+[m[32m        # Canon output units are controlled by get_external_length_units() → 25.4 → mm.[m
[32m+[m[32m        unitcode = "G21"[m
         initcode = INITCODE[m
 [m
         result, seq = gcode.parse(args.gcode_file, bridge, unitcode, initcode)[m
