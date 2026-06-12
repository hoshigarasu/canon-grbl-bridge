"""WebSocket コマンドハンドラ。"""
import asyncio
import os
import re
import threading

from fastapi import WebSocket

from gateway.config import (
    log,
    INTERP_IDLE, INTERP_READING, INTERP_PAUSED,
    TASK_MODE_MANUAL, TASK_MODE_AUTO,
)
from gateway.state import machine
from gateway.clients import clients
from gateway.serial_io import notify_error
from gateway.canon import RS274_AVAILABLE, run_ngc_in_thread
from gateway.toolpath import extract_toolpath, extract_commands
from gateway.settings_store import _load_settings, _save_settings
from gateway import runtime as rt

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
        if rt.serial_bus:
            try:
                await loop.run_in_executor(None, rt.serial_bus.send_cmd, "$X")
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
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"\x18")
        with machine.lock:
            machine.estop   = True
            machine.enabled = False
        return {"type": "reply", "ok": True}

    if cmd == "estop_reset":
        if rt.serial_bus:
            await loop.run_in_executor(None, rt.serial_bus.send_cmd, "$X")
        with machine.lock:
            machine.estop = False
            machine.last_error = None
        return {"type": "reply", "ok": True}

    if cmd == "clear_error":
        with machine.lock:
            machine.last_error = None
        # 全クライアントに「クリアされた」ことを即座に通知
        try:
            await clients.broadcast({"type": "notification", "data": None})
        except Exception as e:
            log.warning(f"clear_error broadcast failed: {e}")
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
                # _wait_ok が原因の通知を既に出している可能性が高いが、
                # 「Run が異常終了した」というイベント自体も通知する。
                # WebUI 側はこれを使ってプログラム停止表示できる。
                notify_error("run_failed", f"Run aborted: {error}")

        with machine.lock:
            machine.interp_state = INTERP_READING
            machine.task_mode    = TASK_MODE_AUTO

        run_ngc_in_thread(ngc, _abort_event, on_done, dry_run=(rt.serial_bus is None))
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

        if rt.serial_bus and cmd_to_send:
            try:
                await loop.run_in_executor(None, rt.serial_bus.send_cmd, cmd_to_send)
                log.info(f"auto_step [{idx+1}/{total}]: {cmd_to_send!r}")
            except Exception as e:
                return {"type": "reply", "ok": False, "error": str(e)}

        return {"type": "reply", "ok": True}
    if cmd == "cycle_pause" or cmd == "feed_hold":
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"!")  # Feed Hold
        with machine.lock:
            machine.interp_state = INTERP_PAUSED
        return {"type": "reply", "ok": True}

    if cmd == "cycle_resume":
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"~")  # Cycle Start / Resume
        with machine.lock:
            machine.interp_state = INTERP_READING
        return {"type": "reply", "ok": True}

    if cmd == "abort":
        _abort_event.set()
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"\x18")  # Soft Reset
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

        if rt.serial_bus and text:
            try:
                result = await loop.run_in_executor(None, rt.serial_bus.send_cmd, text)
                # G10 L20完了後: コマンドからwpos/wcoを直接更新
                if result == "ok" and "G10" in text and "L20" in text:
                    with machine.lock:
                        new_wpos = list(machine.wpos)
                        for ax, idx in (("X",0),("Y",1),("Z",2)):
                            m = re.search(ax + r"([-0-9.]+)", text)
                            if m:
                                new_wpos[idx] = float(m.group(1))
                        machine.wpos = new_wpos
                        machine.wco  = [machine.mpos[i] - machine.wpos[i] for i in range(3)]
                        log.info(f"WCS zero: wpos={machine.wpos} wco={machine.wco}")
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
        if axis < len(letters) and rt.serial_bus:
            feed = abs(vel) * 60.0
            dist = 10000.0 * (1 if vel >= 0 else -1)
            jog_cmd = f"$J=G91 {letters[axis]}{dist:.1f} F{feed:.1f}"
            rt.serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd == "jog_cont_multi":
        axes = msg.get("axes", [])
        letters = ["X", "Y", "Z"]
        if axes and rt.serial_bus:
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
                rt.serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd in ("jog_stop", "jog_stop_multi"):
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"\x85")  # Jog Cancel
        return {"type": "reply", "ok": True}

    if cmd == "jog_incr":
        axis     = msg.get("axis", 0)
        vel      = float(msg.get("vel", 100.0))
        distance = float(msg.get("distance", 1.0))
        letters  = ["X", "Y", "Z"]
        if axis < len(letters) and rt.serial_bus:
            feed = abs(vel) * 60.0          # mm/s → mm/min
            # distance はUIから方向込み（±mm）で来るのでそのまま使う
            jog_cmd = f"$J=G91 {letters[axis]}{distance:.4f} F{feed:.1f}"
            rt.serial_bus.send_jog(jog_cmd)
        return {"type": "reply", "ok": True}

    if cmd == "jog_incr_multi":
        axes    = msg.get("axes", [])
        letters = ["X", "Y", "Z"]
        if axes and rt.serial_bus:
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
                rt.serial_bus.send_jog(jog_cmd)
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
        if rt.serial_bus:
            await loop.run_in_executor(None, rt.serial_bus.send_cmd, f"M3 S{speed:.0f}")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_reverse":
        speed = float(msg.get("speed", 1000))
        if rt.serial_bus:
            await loop.run_in_executor(None, rt.serial_bus.send_cmd, f"M4 S{speed:.0f}")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_stop":
        if rt.serial_bus:
            await loop.run_in_executor(None, rt.serial_bus.send_cmd, "M5")
        return {"type": "reply", "ok": True}

    if cmd == "spindle_increase":
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"\x9A")  # spindle speed +10%
        return {"type": "reply", "ok": True}

    if cmd == "spindle_decrease":
        if rt.serial_bus:
            rt.serial_bus.send_rt(b"\x9B")  # spindle speed -10%
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
