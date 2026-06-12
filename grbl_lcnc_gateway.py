#!/usr/bin/env python3
"""
grbl_lcnc_gateway.py — 互換シム

実装は gateway/ パッケージに移動した (Phase 2 リファクタリング)。
systemd の ExecStart はこのファイルを指し続けるため、このシムを維持する。

  gateway/config.py         定数・ロギング
  gateway/state.py          MachineState
  gateway/runtime.py        起動時初期化される共有シングルトン
  gateway/clients.py        WebSocket クライアント集合
  gateway/serial_io.py      SerialBus / ステータスパース / WS通知
  gateway/canon.py          rs274ngc → grblHAL 変換 (GrblBridge)
  gateway/toolpath.py       ツールパス抽出 / 加工時間推定
  gateway/commands.py       WebSocket コマンドハンドラ
  gateway/http_routes.py    HTTP API ルート
  gateway/settings_store.py 設定永続化
  gateway/app.py            FastAPI アプリ / lifespan / エントリポイント
"""
from gateway.app import app, main  # noqa: F401  (uvicorn import用にappも公開)

if __name__ == "__main__":
    main()
