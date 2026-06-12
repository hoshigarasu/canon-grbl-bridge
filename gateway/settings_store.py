"""WebUI 設定の永続化 (settings.json)。"""
import json
from pathlib import Path

SETTINGS_FILE = Path("/home/arduino/.config/lcnc_gateway/settings.json")


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))
