#!/usr/bin/env python3
"""docs/protocol.md のコマンド表をディスパッチテーブルから再生成する。

使い方:
    python3 tools/gen_protocol.py        # コマンド表 markdown を stdout に出力

新コマンドを commands.py に追加したら、これを実行して
docs/protocol.md の <!-- COMMANDS:START --> 〜 <!-- COMMANDS:END -->
区間を差し替える（手作業 or 下記 --write）。

    python3 tools/gen_protocol.py --write # docs/protocol.md を直接更新
"""
import inspect
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import gateway.commands as c  # noqa: E402

MARK_START = "<!-- COMMANDS:START -->"
MARK_END = "<!-- COMMANDS:END -->"


def build_table() -> str:
    byfn = defaultdict(list)
    for name, fn in c._HANDLERS.items():
        byfn[fn].append(name)

    fns = sorted(byfn.keys(), key=lambda f: f.__code__.co_firstlineno)

    rows = ["| Command(s) | Params | Reply | Fails? | Serial |",
            "|---|---|---|---|---|"]
    for fn in fns:
        names = " / ".join(f"`{n}`" for n in byfn[fn])
        src = inspect.getsource(fn)
        params = sorted(set(re.findall(r'msg\.get\("(\w+)"', src)))
        rtypes = sorted(set(re.findall(r'"type":\s*"(\w+)"', src)))
        has_err = "yes" if 'ok": False' in src else "—"
        sends = "yes" if re.search(
            r'send_cmd|send_jog|send_rt|_serial_cmd|_serial_rt', src) else "—"
        pcol = ", ".join(f"`{p}`" for p in params) if params else "—"
        rcol = ", ".join(rtypes)
        rows.append(f"| {names} | {pcol} | {rcol} | {has_err} | {sends} |")
    return "\n".join(rows)


def main():
    table = build_table()
    if "--write" in sys.argv:
        proto = REPO_ROOT / "docs" / "protocol.md"
        text = proto.read_text()
        new = re.sub(
            re.escape(MARK_START) + r".*?" + re.escape(MARK_END),
            f"{MARK_START}\n{table}\n{MARK_END}",
            text, flags=re.DOTALL)
        proto.write_text(new)
        print(f"Updated {proto}")
    else:
        print(table)


if __name__ == "__main__":
    main()
