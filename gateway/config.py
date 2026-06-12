"""設定定数 + ロギング初期化。"""
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

# リポジトリルート (gateway/ の1つ上)
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORT    = "/dev/ttyHS1"
DEFAULT_BAUD    = 115200
SERIAL_TIMEOUT  = 120
STATUS_POLL_HZ  = 30          # status push レート
POLL_INTERVAL   = 1.0 / STATUS_POLL_HZ
GRBL_POLL_MS    = 100         # ? コマンド送信間隔 (ms)
MM_PER_INCH     = 25.4
INITCODE        = "G17 G40 G49 G80 G90"
NGC_UPLOAD_DIR  = Path("/home/arduino/ngc")
WEBUI_DIST      = REPO_ROOT / "lcnc-webui" / "dist"

# lcnc.ts 定数
INTERP_IDLE     = 1
INTERP_READING  = 2
INTERP_PAUSED   = 3
INTERP_WAITING  = 4
TASK_MODE_MANUAL = 1
TASK_MODE_AUTO   = 2
TASK_MODE_MDI    = 3
