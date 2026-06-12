"""HTTP API ルート (settings / files / grbl-settings / editor)。"""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from gateway.config import log, NGC_UPLOAD_DIR, REPO_ROOT
from gateway.state import machine
from gateway.clients import clients
from gateway.toolpath import extract_commands, _estimate_time
from gateway.settings_store import SETTINGS_FILE, _load_settings, _save_settings
from gateway.commands import _send_viewer_gcode
from gateway import runtime as rt

router = APIRouter()


@router.get("/settings")
async def get_settings():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _load_settings)
    return {"ok": True, "settings": data}


@router.put("/settings/{section}")
@router.post("/settings/{section}")
async def put_settings_section(section: str, request: Request):
    body = await request.json()
    loop = asyncio.get_event_loop()
    def _save():
        data = _load_settings()
        data[section] = body.get("data", body)
        _save_settings(data)
    await loop.run_in_executor(None, _save)
    return {"ok": True}


@router.delete("/settings")
async def delete_settings():
    SETTINGS_FILE.unlink(missing_ok=True)
    return {"ok": True}


@router.get("/gcode")
async def get_gcode_content(path: str):
    """NGCファイルの内容をテキストで返す（GcodePanel表示用）"""
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=file_path.read_text(errors="replace"))


@router.put("/save")
async def save_file(request: Request):
    """GcodePanelのEditで編集したファイルを保存"""
    body = await request.json()
    path    = body.get("path", "")
    content = body.get("content", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    file_path = Path(path)
    # セキュリティ: アップロードディレクトリ配下のみ許可
    try:
        file_path.resolve().relative_to(NGC_UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside upload directory")
    file_path.write_text(content)
    log.info(f"Saved: {file_path} ({len(content)} bytes)")
    # lcncWs._applyGcodeFileは同一パスを無視するため、
    # file=nullを先送りして強制リセットしてからviewer_gcodeを再送する
    await clients.broadcast({"type": "viewer_gcode", "data": {"file": None}})
    asyncio.create_task(_send_viewer_gcode(str(file_path)))
    return {"ok": True, "path": str(file_path), "size": len(content)}


# ── NGC ファイルアップロード ──────────────────────────────────────────
@router.post("/upload")
async def upload_ngc(file: UploadFile = File(...)):
    """
    NGC ファイルをアップロードして active_file に設定。
    viewer_gcode を非同期で生成して全クライアントへ送信。
    """
    filename = Path(file.filename).name
    dest = NGC_UPLOAD_DIR / filename
    content = await file.read()
    dest.write_bytes(content)

    with machine.lock:
        machine.active_file = str(dest)

    asyncio.create_task(_send_viewer_gcode(str(dest)))
    log.info(f"Uploaded: {dest} ({len(content)} bytes)")
    return {"ok": True, "path": str(dest), "filename": filename}


@router.get("/files")
async def list_files(subdir: str = ""):
    """アップロード済みNGCファイル一覧（lcnc-suite FilesResponse形式）"""
    base = NGC_UPLOAD_DIR / subdir if subdir else NGC_UPLOAD_DIR
    entries = []
    if base.exists():
        for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if p.is_dir():
                entries.append({
                    "name": p.name,
                    "type": "directory",
                    "path": str(p),
                })
            elif p.suffix.lower() in (".ngc", ".nc", ".gcode", ".tap", ".txt"):
                entries.append({
                    "name": p.name,
                    "type": "file",
                    "path": str(p),
                    "size": p.stat().st_size,
                    "modified": int(p.stat().st_mtime),
                })
    return {
        "ok":      True,
        "nc_dir":  str(NGC_UPLOAD_DIR),
        "subdir":  subdir,
        "entries": entries,
    }


@router.post("/telemetry")
async def telemetry(request: Request):
    """lcnc-webui からのテレメトリを受け取る（ログのみ）"""
    return JSONResponse({"ok": True})


# ── grblHAL settings UI ────────────────────────────────────────
@router.get("/grbl-settings")
async def _grbl_settings_page():
    from fastapi.responses import HTMLResponse as _HR
    return _HR((REPO_ROOT / "grbl-settings.html").read_text())


@router.get("/grbl-settings-data")
async def _grbl_settings_get():
    if not rt.serial_bus:
        return JSONResponse({"ok": False, "error": "no serial"})
    import re as _re
    loop = asyncio.get_event_loop()
    try:
        lines = await loop.run_in_executor(None, rt.serial_bus.send_and_collect, "$$")
        settings = []
        for ln in lines:
            m = _re.match(r'\$(\d+)=([^\s(]+)(?:\s+\(([^)]*)\))?', ln)
            if m:
                settings.append({"id": int(m.group(1)), "value": m.group(2), "desc": m.group(3) or ""})
        return JSONResponse({"ok": True, "settings": settings})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/grbl-settings-data")
async def _grbl_settings_set(request: Request):
    body = await request.json()
    n, v = body.get("n"), body.get("v")
    if n is None or v is None:
        return JSONResponse({"ok": False, "error": "missing n or v"})
    if not rt.serial_bus:
        return JSONResponse({"ok": False, "error": "no serial"})
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, rt.serial_bus.send_cmd, f"${n}={v}")
        return JSONResponse({"ok": result == "ok", "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ── 推定加工時間 ─────────────────────────────────────────────────
@router.get("/estimate-time")
async def estimate_time_ep(path: str = ""):
    import pathlib as _pl
    if not path:
        with machine.lock:
            path = machine.active_file or ""
    if not path or not _pl.Path(path).exists():
        return JSONResponse({"ok": False, "error": "no file"})
    try:
        _loop = asyncio.get_event_loop()
        cmds = await _loop.run_in_executor(None, extract_commands, path)
        secs = _estimate_time(cmds)
        m, s = divmod(int(secs), 60)
        return JSONResponse({"ok": True, "seconds": round(secs),
                             "formatted": f"{m}m {s:02d}s"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/status-summary")
async def _status_summary():
    with machine.lock:
        return JSONResponse({
            "active_file":  machine.active_file or "",
            "interp_state": machine.interp_state,
        })


# ── G-code popup editor ──────────────────────────────────────────
from fastapi.responses import HTMLResponse as _HtmlR

_EDITOR_DIR = REPO_ROOT


@router.get("/active-file")
async def _active_file():
    with machine.lock:
        f = machine.active_file
    return JSONResponse({"path": f or ""})


@router.get("/read-file")
async def _read_file(path: str):
    from fastapi.responses import PlainTextResponse
    p = Path(path)
    if not p.exists() or p.suffix.lower() not in {".ngc",".nc",".gcode",".tap",".txt"}:
        return JSONResponse({"error":"not found"}, status_code=404)
    return PlainTextResponse(p.read_text(errors="replace"))


@router.get("/editor")
async def _editor():
    return _HtmlR((_EDITOR_DIR / "editor.html").read_text())


@router.get("/editor-widget.js")
async def _editor_widget():
    from fastapi.responses import Response
    return Response((_EDITOR_DIR / "editor-widget.js").read_text(), media_type="application/javascript")
