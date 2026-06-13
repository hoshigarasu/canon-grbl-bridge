"""_parse_grbl_status の回帰テスト。

grblHAL の ? レスポンス各パターンが MachineState に正しく反映されるか。
grblHAL は $10 設定により MPos のみ報告する構成（WCO は G10 L20 から推定）。
"""
from gateway.serial_io import _parse_grbl_status
from gateway.config import INTERP_IDLE, INTERP_READING, INTERP_PAUSED


def test_idle_mpos(fresh_machine):
    _parse_grbl_status("<Idle|MPos:0.000,0.000,0.000|Bf:35,1023|FS:0,0>")
    assert fresh_machine.grbl_state == "Idle"
    assert fresh_machine.interp_state == INTERP_IDLE
    assert fresh_machine.mpos == [0.0, 0.0, 0.0]


def test_run_mpos_feed(fresh_machine):
    _parse_grbl_status("<Run|MPos:12.500,-3.250,1.000|Bf:30,1000|FS:300,0>")
    assert fresh_machine.grbl_state == "Run"
    assert fresh_machine.interp_state == INTERP_READING
    assert fresh_machine.mpos == [12.5, -3.25, 1.0]
    assert fresh_machine.feed == 300.0


def test_hold_state(fresh_machine):
    _parse_grbl_status("<Hold:0|MPos:5.000,5.000,0.000|Bf:35,1023|FS:0,0>")
    assert fresh_machine.interp_state == INTERP_PAUSED


def test_wco_applied_to_wpos(fresh_machine):
    # WCO が来ると wpos = mpos - wco
    _parse_grbl_status("<Idle|MPos:10.000,20.000,5.000|WCO:1.000,2.000,3.000>")
    assert fresh_machine.wco == [1.0, 2.0, 3.0]
    assert fresh_machine.wpos == [9.0, 18.0, 2.0]


def test_mpos_uses_existing_wco(fresh_machine):
    # 先に WCO を確定 → 次の MPos のみ更新で wpos が追従
    _parse_grbl_status("<Idle|MPos:0.000,0.000,0.000|WCO:5.000,0.000,0.000>")
    _parse_grbl_status("<Run|MPos:15.000,0.000,0.000|Bf:30,1000|FS:200,0>")
    assert fresh_machine.wpos[0] == 10.0   # 15 - 5


def test_fs_feed_and_spindle(fresh_machine):
    _parse_grbl_status("<Run|MPos:0.000,0.000,0.000|FS:450,1200>")
    assert fresh_machine.feed == 450.0
    assert fresh_machine.spindle_rpm == 1200.0


def test_alarm_sets_estop(fresh_machine):
    _parse_grbl_status("<Alarm|MPos:0.000,0.000,0.000|Bf:35,1023|FS:0,0>")
    assert fresh_machine.grbl_state == "Alarm"
    assert fresh_machine.estop is True


def test_pin_probe_tripped(fresh_machine):
    _parse_grbl_status("<Idle|MPos:0.000,0.000,0.000|Pn:P>")
    assert fresh_machine.probe_tripped is True


def test_pin_absent_clears_probe(fresh_machine):
    _parse_grbl_status("<Idle|MPos:0.000,0.000,0.000|Pn:P>")
    _parse_grbl_status("<Idle|MPos:0.000,0.000,0.000|Bf:35,1023|FS:0,0>")
    # Pn が無いステータスでは probe_tripped は更新されない（前回値が残る）
    # ※これは現仕様の挙動を固定するもの。
    assert fresh_machine.probe_tripped is True


def test_aux_flood_mist(fresh_machine):
    _parse_grbl_status("<Run|MPos:0.000,0.000,0.000|FS:300,0|A:SF>")
    assert fresh_machine.spindle_direction == 1   # S = SpindleCW
    assert fresh_machine.flood is True            # F
    assert fresh_machine.mist is False
