#!/bin/bash
# =============================================================================
# smoke_test.sh — canon-grbl-bridge リファクタリング用スモークテスト
# 実行場所: UNO Q box上 (ssh uno-q '~/canon-grbl-bridge/smoke_test.sh')
#
# 確認項目:
#   1. gateway サービス active
#   2. HTTP :8000 応答
#   3. WebSocket 接続 → viewer_init 受信
#   4. heartbeat → pong
#   5. grblHAL シリアル往復 ($$ 設定取得)
#   6. NGC アップロード → viewer_gcode 受信
#   7. (--with-motion 時のみ) X軸 ±0.1mm ジョグ
#
# 終了コード: 0=全パス, 1=失敗
# =============================================================================
set -u

HOST="${SMOKE_HOST:-127.0.0.1}"
PORT="${SMOKE_PORT:-8000}"
BASE="http://${HOST}:${PORT}"
WITH_MOTION=0
[[ "${1:-}" == "--with-motion" ]] && WITH_MOTION=1

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
PASS=0; FAIL=0
ok()   { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
ng()   { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }

# --- 依存: websockets (なければ導入) ----------------------------------------
python3 -c "import websockets" 2>/dev/null || {
    echo "[INFO] installing websockets..."
    pip3 install websockets --break-system-packages -q || { ng "websockets install"; exit 1; }
}

# --- 1. サービス active -------------------------------------------------------
if systemctl is-active --quiet grbl-lcnc-gateway; then
    ok "service active"
else
    ng "service not active"; exit 1
fi

# --- 2. HTTP 応答 (再起動直後はlisten開始まで待つ: 最大15秒) ------------------
HTTP_OK=0
for i in $(seq 1 15); do
    if curl -sf -o /dev/null --max-time 2 "${BASE}/files"; then
        HTTP_OK=1; break
    fi
    sleep 1
done
if [[ $HTTP_OK -eq 1 ]]; then
    ok "HTTP /files responds"
else
    ng "HTTP /files no response (15s timeout)"; exit 1
fi

# --- 5. grblHAL シリアル往復 ($$ 取得) -----------------------------------------
GS=$(curl -sf --max-time 15 "${BASE}/grbl-settings-data" || true)
if [[ -n "$GS" && "$GS" == *'"'* ]]; then
    ok "grblHAL serial round-trip (\$\$)"
else
    ng "grblHAL serial round-trip failed: '$GS'"
fi

# --- 3,4,6,7. WebSocket テスト (Python) ----------------------------------------
python3 - "$HOST" "$PORT" "$WITH_MOTION" <<'PYEOF'
import asyncio, json, sys
import websockets

HOST, PORT, WITH_MOTION = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
URI = f"ws://{HOST}:{PORT}/ws"
NGC = "/home/arduino/canon-grbl-bridge/examples/circle_demo.ngc"

GREEN, RED, NC = "\033[0;32m", "\033[0;31m", "\033[0m"
results = []
def ok(m):  results.append(True);  print(f"{GREEN}[PASS]{NC} {m}")
def ng(m):  results.append(False); print(f"{RED}[FAIL]{NC} {m}")

async def recv_until(ws, want_type, timeout=10):
    """want_type のメッセージが来るまで読み捨てる"""
    end = asyncio.get_event_loop().time() + timeout
    while True:
        remain = end - asyncio.get_event_loop().time()
        if remain <= 0:
            raise TimeoutError(want_type)
        raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        msg = json.loads(raw)
        if msg.get("type") == want_type:
            return msg

async def main():
    async with websockets.connect(URI, max_size=50*1024*1024) as ws:
        # 3. viewer_init
        try:
            await recv_until(ws, "viewer_init", timeout=5)
            ok("WS connect + viewer_init")
        except TimeoutError:
            ng("viewer_init not received")

        # 4. heartbeat → pong
        await ws.send(json.dumps({"cmd": "heartbeat"}))
        try:
            await recv_until(ws, "pong", timeout=5)
            ok("heartbeat → pong")
        except TimeoutError:
            ng("pong not received")

        # 6. load_file → viewer_gcode
        await ws.send(json.dumps({"cmd": "load_file", "path": NGC}))
        try:
            await recv_until(ws, "viewer_gcode", timeout=20)
            ok("load_file → viewer_gcode")
        except TimeoutError:
            ng("viewer_gcode not received")

        # 7. motion (optional)
        if WITH_MOTION:
            await ws.send(json.dumps(
                {"cmd": "jog_incr", "axis": 0, "vel": 10.0, "distance": 0.1}))
            await asyncio.sleep(1.5)
            await ws.send(json.dumps(
                {"cmd": "jog_incr", "axis": 0, "vel": 10.0, "distance": -0.1}))
            await asyncio.sleep(1.5)
            ok("jog X +0.1/-0.1 sent (verify Idle on WebUI)")

asyncio.run(main())
sys.exit(0 if all(results) else 1)
PYEOF
WS_RC=$?
[[ $WS_RC -ne 0 ]] && FAIL=$((FAIL+1))

# --- 結果 -----------------------------------------------------------------------
echo "─────────────────────────────"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}SMOKE TEST: ALL PASS${NC}"
    exit 0
else
    echo -e "${RED}SMOKE TEST: ${FAIL} FAILURE(S)${NC}"
    exit 1
fi
