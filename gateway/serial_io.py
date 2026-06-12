"""ttyHS1 シリアル通信 + grblHAL ステータスパース + WS通知。"""
import asyncio
import threading
import time
from typing import Optional

import serial

from gateway.config import (
    log, SERIAL_TIMEOUT,
    INTERP_IDLE, INTERP_READING, INTERP_PAUSED,
    TASK_MODE_MANUAL, TASK_MODE_AUTO,
)
from gateway.state import machine
from gateway.clients import clients
from gateway import runtime as rt


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
                if resp.startswith("[MSG:"):
                    notify_msg(resp[5:].rstrip("]"))
                else:
                    log.debug(f"[grbl msg] {resp}")
                continue
            if resp.startswith("ALARM"):
                log.warning(f"grblHAL ALARM: {resp!r} (sent: {sent!r})")
                with machine.lock:
                    machine.grbl_state = "Alarm"
                    machine.estop = True
                notify_error("alarm", f"grblHAL {resp}", sent=sent, code=resp)
                return resp
            if resp.startswith("error"):
                log.warning(f"grblHAL error: {resp!r} (sent: {sent!r})")
                notify_error("error", f"grblHAL {resp}", sent=sent, code=resp)
                return resp
            if resp == "ok":
                return "ok"
            if resp.startswith("<") and resp.endswith(">"):
                _parse_grbl_status(resp)
                continue
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
        """? をRTコマンドとして送信（ロック不要）。レスポンスはロック空き時のみ直接読む。"""
        self.ser.write(b"?")
        if not self._cmd_lock.locked():
            with self._cmd_lock:
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
                machine.wpos = [machine.mpos[j] - machine.wco[j] for j in range(3)]
            elif p.startswith("WPos:"):
                vals = p[5:].split(",")
                machine.wpos = [float(v) for v in vals[:3]]
                log.info(f"WPos→wpos: {machine.wpos}")
            elif p.startswith("WCO:"):
                vals = p[4:].split(",")
                machine.wco = [float(v) for v in vals[:3]]
                machine.wpos = [machine.mpos[i] - machine.wco[i] for i in range(3)]
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


def notify_error(severity: str, message: str, sent: str = "", code: Optional[str] = None):
    """
    grblHAL の拒否や run 失敗を WebSocket クライアント全員に通知する。
    machine.last_error にも保存するので、後から接続したクライアントも見える。
    どのスレッドからでも呼べる（asyncio.run_coroutine_threadsafe 経由）。

    severity: "error" | "alarm" | "timeout" | "abort" | "run_failed"
    code:     grblHAL の応答そのまま（例 "error:33", "ALARM:9"）
    """
    err = {
        "ts":       time.time(),
        "severity": severity,
        "message":  message,
        "sent":     sent,
        "code":     code,
    }
    with machine.lock:
        machine.last_error = err
    log.info(f"notify_error: {severity} {message!r} sent={sent!r}")
    if rt.event_loop is not None and rt.event_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                clients.broadcast({"type": "notification", "data": err}),
                rt.event_loop
            )
        except Exception as e:
            log.warning(f"notify_error broadcast failed: {e}")


def notify_msg(text: str):
    """
    grblHAL の [MSG:...] をWebSocketクライアントに転送する。
    どのスレッドからでも呼べる（asyncio.run_coroutine_threadsafe 経由）。
    """
    log.info(f"grbl msg: {text!r}")
    if rt.event_loop is not None and rt.event_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                clients.broadcast({"type": "grbl_msg", "data": {"text": text}}),
                rt.event_loop
            )
        except Exception as e:
            log.warning(f"notify_msg broadcast failed: {e}")
