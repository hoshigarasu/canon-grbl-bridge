# WebSocket / HTTP Protocol

canon-grbl-bridge gateway と lcnc-suite WebUI 間の通信仕様。
WebSocket エンドポイントは `/ws`。クライアントは JSON テキストフレームを送り、
gateway は JSON で応答する。状態は別途 30 Hz の `status` ブロードキャストで配信される。

## Connection lifecycle

1. クライアントが `/ws` に接続
2. gateway が即座に `viewer_init` を送信（軸構成・運動学・機械境界）
3. ロード済みファイルがあれば `viewer_gcode` を送信
4. 以降、クライアントが `{"cmd": "..."}` を送るたびに gateway が `reply` を返す
5. 並行して `status` が 30 Hz で全クライアントへ配信される

## Server-initiated messages (broadcast)

| type | 内容 |
|---|---|
| `viewer_init` | 接続直後。軸・運動学・機械境界 |
| `viewer_gcode` | ツールパス（feed/rapid 座標列）。ファイルロード時 |
| `status` | 機械状態スナップショット。30 Hz |
| `notification` | エラー通知（grblHAL 拒否 / ALARM / run 失敗）。`data: null` でクリア |
| `grbl_msg` | grblHAL `[MSG:...]` の転送（M816 センサーレポート等） |

## Client commands

下表は `gateway/commands.py` のディスパッチテーブルから自動生成
（`python3 tools/gen_protocol.py` で再生成）。

- **Params**: `msg` から読むキー（`cmd` 以外）
- **Reply**: 応答の `type`
- **Fails?**: `{"ok": false, "error": ...}` を返しうるか
- **Serial**: grblHAL へシリアル送信するか

<!-- COMMANDS:START -->
| Command(s) | Params | Reply | Fails? | Serial |
|---|---|---|---|---|
| `heartbeat` | — | pong | — | — |
| `tab_visibility` | — | reply | — | — |
| `arm` | `armed` | reply | — | — |
| `machine_on` | — | reply | — | yes |
| `machine_off` | — | reply | — | — |
| `estop` | — | reply | — | yes |
| `estop_reset` | — | reply | — | yes |
| `clear_error` | — | notification, reply | — | — |
| `auto_run` / `cycle_start` | — | reply | yes | — |
| `auto_step` | — | reply | yes | yes |
| `cycle_pause` / `feed_hold` | — | reply | — | yes |
| `cycle_resume` | — | reply | — | yes |
| `abort` | — | reply | — | yes |
| `mdi` | `text` | reply | yes | yes |
| `jog_cont` | `axis`, `vel` | reply | — | yes |
| `jog_cont_multi` | `axes` | reply | — | yes |
| `jog_stop` / `jog_stop_multi` | — | reply | — | yes |
| `jog_incr` | `axis`, `distance`, `vel` | reply | — | yes |
| `jog_incr_multi` | `axes` | reply | — | yes |
| `set_feed_override` | `scale` | reply | — | — |
| `load_file` | `path` | reply | yes | — |
| `unload_file` | — | reply | — | — |
| `home` / `home_all` / `unhome_all` | — | reply | — | — |
| `spindle_forward` | `speed` | reply | — | yes |
| `spindle_reverse` | `speed` | reply | — | yes |
| `spindle_stop` | — | reply | — | yes |
| `spindle_increase` | — | reply | — | yes |
| `spindle_decrease` | — | reply | — | yes |
| `save_settings` | `data`, `section` | reply | — | — |
| `timing_log` | — | reply | — | — |
| `get_tool_table` | — | reply | — | — |
| `tool_change` / `save_tool` / `add_tool` / `delete_tool` / `set_optional_stop` / `set_block_delete` / `set_mode` | — | reply | — | — |
<!-- COMMANDS:END -->

## Reply shape

通常応答:
```json
{"type": "reply", "ok": true}
```

失敗応答:
```json
{"type": "reply", "ok": false, "error": "No file loaded"}
```

特殊:
- `heartbeat` → `{"type": "pong"}`
- `arm` → `{"type": "reply", "ok": true, "armed": <bool>}`
- `get_tool_table` → `{"type": "reply", "ok": true, "tool_table": []}`

## Notes

- ジョグは `$J=` を即時送信し ok を待たない（`send_jog`）。`jog_stop` は Jog Cancel (0x85)。
- `estop` / `abort` は Soft Reset (0x18) を送る。`machine_off` は送らない（モーター無効化のみ）。
- `mdi` は LinuxCNC 固有表現（`O<go_to_zero>` 等）を grblHAL 互換へ変換し、`G10 L20` 後は WCO を再計算する。
- ホーミングは grblHAL `$22=0` 前提のため `home*` は no-op。
