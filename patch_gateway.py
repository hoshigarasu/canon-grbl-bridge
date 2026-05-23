import sys

# ── send_and_collect メソッド追加 ──────────────────────────────────
METHOD = (
    "    def send_and_collect(self, cmd: str) -> list:\n"
    "        \"\"\"コマンドを送信し $始まりの複数行を収集してokを待つ（$$ 用）\"\"\"\n"
    "        with self._cmd_lock:\n"
    "            self._drain_input()\n"
    "            self.ser.write((cmd.strip() + \"\\n\").encode())\n"
    "            lines = []\n"
    "            deadline = time.time() + SERIAL_TIMEOUT\n"
    "            while time.time() < deadline:\n"
    "                resp = self.ser.readline().decode(errors=\"replace\").strip()\n"
    "                if not resp:\n"
    "                    continue\n"
    "                if resp == \"ok\":\n"
    "                    return lines\n"
    "                if resp.startswith(\"error\") or resp.startswith(\"ALARM\"):\n"
    "                    raise RuntimeError(resp)\n"
    "                if resp.startswith(\"$\"):\n"
    "                    lines.append(resp)\n"
    "                else:\n"
    "                    log.debug(f\"[collect] {resp!r}\")\n"
    "            raise TimeoutError(f\"no response for: {cmd!r}\")\n"
    "\n"
)

# ── grblHAL settings エンドポイント追加 ───────────────────────────
ROUTES = (
    "# ── grblHAL settings UI ────────────────────────────────────────\n"
    "import pathlib as _pl2\n"
    "\n"
    "@app.get(\"/grbl-settings\")\n"
    "async def _grbl_settings_page():\n"
    "    from fastapi.responses import HTMLResponse as _HR\n"
    "    return _HR((_pl2.Path(__file__).parent / \"grbl-settings.html\").read_text())\n"
    "\n"
    "@app.get(\"/grbl-settings-data\")\n"
    "async def _grbl_settings_get():\n"
    "    if not serial_bus:\n"
    "        return JSONResponse({\"ok\": False, \"error\": \"no serial\"})\n"
    "    import re as _re\n"
    "    loop = asyncio.get_event_loop()\n"
    "    try:\n"
    "        lines = await loop.run_in_executor(None, serial_bus.send_and_collect, \"$$\")\n"
    "        settings = []\n"
    "        for ln in lines:\n"
    "            m = _re.match(r'\\$(\\d+)=([^\\s(]+)(?:\\s+\\(([^)]*)\\))?', ln)\n"
    "            if m:\n"
    "                settings.append({\"id\": int(m.group(1)), \"value\": m.group(2), \"desc\": m.group(3) or \"\"})\n"
    "        return JSONResponse({\"ok\": True, \"settings\": settings})\n"
    "    except Exception as e:\n"
    "        return JSONResponse({\"ok\": False, \"error\": str(e)})\n"
    "\n"
    "@app.post(\"/grbl-settings-data\")\n"
    "async def _grbl_settings_set(request: Request):\n"
    "    body = await request.json()\n"
    "    n, v = body.get(\"n\"), body.get(\"v\")\n"
    "    if n is None or v is None:\n"
    "        return JSONResponse({\"ok\": False, \"error\": \"missing n or v\"})\n"
    "    if not serial_bus:\n"
    "        return JSONResponse({\"ok\": False, \"error\": \"no serial\"})\n"
    "    loop = asyncio.get_event_loop()\n"
    "    try:\n"
    "        result = await loop.run_in_executor(None, serial_bus.send_cmd, f\"${n}={v}\")\n"
    "        return JSONResponse({\"ok\": result == \"ok\", \"result\": result})\n"
    "    except Exception as e:\n"
    "        return JSONResponse({\"ok\": False, \"error\": str(e)})\n"
    "\n"
)

MARKER_METHOD = "    def poll_status(self):"
MARKER_ROUTES = "# ── G-code popup editor"

with open("grbl_lcnc_gateway.py", "r") as f:
    src = f.read()

errors = []
if MARKER_METHOD not in src:
    errors.append("ERROR: poll_status marker not found")
if MARKER_ROUTES not in src:
    errors.append("ERROR: popup editor marker not found")
if "send_and_collect" in src:
    errors.append("SKIP: send_and_collect already exists")
if "/grbl-settings" in src:
    errors.append("SKIP: grbl-settings routes already exist")

if errors:
    for e in errors:
        print(e)
    sys.exit(1)

src = src.replace(MARKER_METHOD, METHOD + MARKER_METHOD, 1)
src = src.replace(MARKER_ROUTES, ROUTES + MARKER_ROUTES, 1)

with open("grbl_lcnc_gateway.py", "w") as f:
    f.write(src)

# 構文チェック
import subprocess
result = subprocess.run(["python3", "-m", "py_compile", "grbl_lcnc_gateway.py"], capture_output=True, text=True)
if result.returncode != 0:
    print("SYNTAX ERROR:", result.stderr)
    sys.exit(1)

print("OK: patch applied, syntax OK")
