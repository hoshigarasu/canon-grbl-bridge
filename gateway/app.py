"""FastAPI アプリ本体 — lifespan / status loop / WebSocket / 静的配信。"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from gateway.config import (
    log, DEFAULT_PORT, DEFAULT_BAUD,
    GRBL_POLL_MS, POLL_INTERVAL, NGC_UPLOAD_DIR, WEBUI_DIST,
)
from gateway.state import machine
from gateway.clients import clients
from gateway.serial_io import SerialBus
from gateway.commands import handle_command, _send_viewer_gcode
from gateway.http_routes import router
from gateway import runtime as rt


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
        if now >= next_poll and rt.serial_bus is not None:
            try:
                await loop.run_in_executor(None, rt.serial_bus.poll_status)
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
# FastAPI アプリ
# ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    rt.event_loop = asyncio.get_running_loop()
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
        rt.serial_bus = None
    else:
        try:
            rt.serial_bus = SerialBus(port, DEFAULT_BAUD)
            # 初期化: アラームクリア + ワーク原点設定
            rt.serial_bus.send_cmd("$X")
            time.sleep(0.2)
            rt.serial_bus.send_cmd("G92 X0 Y0 Z0")
            log.info("grblHAL initialized")
        except Exception as e:
            log.warning(f"Serial unavailable ({e}), running in dry-run mode")
            rt.serial_bus = None

    # status ループ起動
    loop_task = asyncio.create_task(status_loop())

    yield  # アプリ実行

    loop_task.cancel()
    if rt.serial_bus:
        rt.serial_bus.close()
    if _gpio_request:
        try:
            _gpio_request.release()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)
app.include_router(router)


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
            "groups": [{"id": "tool", "parent": "root"}], "parts": [], "kinematics": [{"group": "tool", "joint": 0, "type": "translate", "direction": "x", "sign": 1}, {"group": "tool", "joint": 1, "type": "translate", "direction": "y", "sign": 1}, {"group": "tool", "joint": 2, "type": "translate", "direction": "z", "sign": 1}],
            "workGroup": "root", "toolGroup": "tool",
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
def main():
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
