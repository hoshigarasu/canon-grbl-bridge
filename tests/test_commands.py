"""handle_command ディスパッチテーブルの回帰テスト。

_HANDLERS を直接叩き、reply 形状と副作用 (machine 状態 / シリアル送信) を検証。
ws 引数は全ハンドラで未使用なので None を渡す。
"""
import pytest

from gateway.commands import handle_command, _HANDLERS
from gateway.config import (
    INTERP_IDLE, INTERP_READING, INTERP_PAUSED,
    TASK_MODE_MANUAL,
)

pytestmark = pytest.mark.asyncio


# ── ディスパッチ機構そのもの ─────────────────────────────────────────
async def test_unknown_command(fresh_machine):
    r = await handle_command(None, {"cmd": "does_not_exist"})
    assert r == {"type": "reply", "ok": False, "error": "Unknown command: does_not_exist"}


async def test_handler_count():
    # 32 ユニーク関数 / 43 登録名 (Phase 3 確定値)
    assert len(_HANDLERS) == 43
    assert len({fn.__name__ for fn in _HANDLERS.values()}) == 32


async def test_home_aliases_map_to_home_handler():
    # home_all / unhome_all は cmd_home に登録 (noop_batch ではない)
    assert _HANDLERS["home"].__name__ == "cmd_home"
    assert _HANDLERS["home_all"].__name__ == "cmd_home"
    assert _HANDLERS["unhome_all"].__name__ == "cmd_home"


# ── reply 形状 ───────────────────────────────────────────────────────
async def test_heartbeat_returns_pong(fresh_machine):
    assert await handle_command(None, {"cmd": "heartbeat"}) == {"type": "pong"}


async def test_arm_includes_armed_key(fresh_machine):
    r = await handle_command(None, {"cmd": "arm", "armed": True})
    assert r == {"type": "reply", "ok": True, "armed": True}
    assert fresh_machine.armed is True


async def test_arm_default_true(fresh_machine):
    r = await handle_command(None, {"cmd": "arm"})
    assert r["armed"] is True


async def test_get_tool_table_shape(fresh_machine):
    r = await handle_command(None, {"cmd": "get_tool_table"})
    assert r == {"type": "reply", "ok": True, "tool_table": []}


async def test_noop_batch(fresh_machine):
    for cmd in ("tool_change", "save_tool", "add_tool", "delete_tool",
                "set_optional_stop", "set_block_delete", "set_mode"):
        r = await handle_command(None, {"cmd": cmd})
        assert r == {"type": "reply", "ok": True}


# ── 機械電源 / ESTOP の副作用 ────────────────────────────────────────
async def test_machine_on(fresh_machine, fake_serial):
    r = await handle_command(None, {"cmd": "machine_on"})
    assert r["ok"] is True
    assert fresh_machine.enabled is True
    assert fresh_machine.estop is False
    assert "$X" in fake_serial.sent


async def test_machine_off_does_not_reset(fresh_machine, fake_serial):
    # Machine Off はモーター無効化のみ、Soft Reset を送らない
    await handle_command(None, {"cmd": "machine_off"})
    assert fresh_machine.enabled is False
    assert b"\x18" not in fake_serial.rt


async def test_estop_sends_soft_reset(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "estop"})
    assert b"\x18" in fake_serial.rt
    assert fresh_machine.estop is True
    assert fresh_machine.enabled is False


async def test_estop_reset_clears(fresh_machine, fake_serial):
    fresh_machine.estop = True
    fresh_machine.last_error = {"severity": "alarm"}
    await handle_command(None, {"cmd": "estop_reset"})
    assert fresh_machine.estop is False
    assert fresh_machine.last_error is None
    assert "$X" in fake_serial.sent


# ── 実行制御 ─────────────────────────────────────────────────────────
async def test_auto_run_no_file(fresh_machine, fake_serial):
    fresh_machine.active_file = None
    r = await handle_command(None, {"cmd": "auto_run"})
    assert r == {"type": "reply", "ok": False, "error": "No file loaded"}


async def test_cycle_pause_feed_hold(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "cycle_pause"})
    assert b"!" in fake_serial.rt
    assert fresh_machine.interp_state == INTERP_PAUSED


async def test_cycle_resume(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "cycle_resume"})
    assert b"~" in fake_serial.rt
    assert fresh_machine.interp_state == INTERP_READING


async def test_abort_resets_step_state(fresh_machine, fake_serial):
    fresh_machine.step_commands = ["G0 X1", "G1 Y2"]
    fresh_machine.step_index = 1
    await handle_command(None, {"cmd": "abort"})
    assert b"\x18" in fake_serial.rt
    assert fresh_machine.step_commands == []
    assert fresh_machine.step_index == 0
    assert fresh_machine.interp_state == INTERP_IDLE


# ── MDI / WCS ────────────────────────────────────────────────────────
async def test_mdi_wcs_tracking(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "mdi", "text": "G55"})
    assert fresh_machine.wcs_p == 2
    assert "G55" in fake_serial.sent


async def test_mdi_go_to_zero_translation(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "mdi", "text": "O<go_to_zero>"})
    assert "G0 X0 Y0 Z0" in fake_serial.sent


async def test_mdi_g10_l20_updates_wco(fresh_machine, fake_serial):
    fresh_machine.mpos = [10.0, 20.0, 0.0]
    await handle_command(None, {"cmd": "mdi", "text": "G10 L20 P1 X0 Y0 Z0"})
    # wpos=0,0,0 に設定 → wco = mpos - wpos = mpos
    assert fresh_machine.wpos == [0.0, 0.0, 0.0]
    assert fresh_machine.wco == [10.0, 20.0, 0.0]


# ── ジョグ ───────────────────────────────────────────────────────────
async def test_jog_incr_format(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "jog_incr", "axis": 0, "vel": 10.0, "distance": 0.1})
    assert len(fake_serial.jogs) == 1
    j = fake_serial.jogs[0]
    assert j.startswith("$J=G91 X0.1000 F600.0")  # vel 10mm/s * 60 = 600mm/min


async def test_jog_incr_negative_distance(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "jog_incr", "axis": 1, "vel": 5.0, "distance": -2.0})
    assert "Y-2.0000" in fake_serial.jogs[0]


async def test_jog_stop_sends_cancel(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "jog_stop"})
    assert b"\x85" in fake_serial.rt


async def test_jog_incr_multi(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "jog_incr_multi", "axes": [
        {"axis": 0, "vel": 10.0, "distance": 1.0},
        {"axis": 1, "vel": 5.0,  "distance": 2.0},
    ]})
    j = fake_serial.jogs[0]
    assert "X1.0000" in j and "Y2.0000" in j
    assert "F600.0" in j   # max(10,5)*60


# ── スピンドル ───────────────────────────────────────────────────────
async def test_spindle_forward(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "spindle_forward", "speed": 1500})
    assert "M3 S1500" in fake_serial.sent


async def test_spindle_stop(fresh_machine, fake_serial):
    await handle_command(None, {"cmd": "spindle_stop"})
    assert "M5" in fake_serial.sent


# ── シリアル未接続でも落ちない ───────────────────────────────────────
async def test_commands_survive_no_serial(fresh_machine, no_serial):
    # rt.serial_bus is None でも reply を返す（送信はスキップ）
    for req in (
        {"cmd": "machine_on"},
        {"cmd": "estop"},
        {"cmd": "jog_incr", "axis": 0, "vel": 10.0, "distance": 1.0},
        {"cmd": "spindle_forward", "speed": 1000},
        {"cmd": "cycle_pause"},
    ):
        r = await handle_command(None, req)
        assert r["type"] in ("reply", "pong")
