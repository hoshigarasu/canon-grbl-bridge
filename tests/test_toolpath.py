"""_estimate_time の回帰テスト。

rs274ngc (gcode) が無い CI でも動くよう、コマンドリストを直接渡す。
"""
from gateway.toolpath import _estimate_time


def test_empty():
    assert _estimate_time([]) == 0.0


def test_rapid_only():
    # G0 で 60mm 移動、rapid 3000mm/min → 60/3000*60 = 1.2s
    secs = _estimate_time(["G0 X60 Y0 Z0"], rapid_mm_min=3000.0)
    assert abs(secs - 1.2) < 1e-6


def test_feed_move():
    # G1 X100 @ F1000mm/min → 100/1000*60 = 6.0s
    secs = _estimate_time(["G1 X100 Y0 Z0 F1000"])
    assert abs(secs - 6.0) < 1e-6


def test_feed_persists_across_lines():
    # F は一度設定したら次行へ継続
    secs = _estimate_time(["G1 X100 F1000", "G1 X200"])
    # 100mm + 100mm @ 1000 = 6 + 6
    assert abs(secs - 12.0) < 1e-6


def test_dwell_ms():
    # G4 P2000 (grblHAL は ms) → 2.0s
    secs = _estimate_time(["G4 P2000"])
    assert abs(secs - 2.0) < 1e-6


def test_arc_quarter_circle():
    import math
    # 半径10の1/4円弧。CCW (G3) で (10,0)→(0,10)、中心(0,0)
    # 弧長 = 2πr/4 = π*10/2 ≈ 15.708mm @ F1000 → /1000*60
    cmds = ["G1 X10 Y0 F1000", "G3 X0 Y10 I-10 J0"]
    secs = _estimate_time(cmds)
    expect_line = 10 / 1000 * 60
    expect_arc  = (math.pi * 10 / 2) / 1000 * 60
    assert abs(secs - (expect_line + expect_arc)) < 1e-3


def test_mixed_program():
    cmds = [
        "G0 X0 Y0 Z5",       # rapid (ゼロ距離に近い)
        "G1 Z-1 F300",       # plunge 6mm? no, 5→-1 = 6mm @300
        "G1 X50 F600",       # 50mm @600
        "G0 Z5",             # retract rapid
    ]
    secs = _estimate_time(cmds, rapid_mm_min=3000.0)
    assert secs > 0
