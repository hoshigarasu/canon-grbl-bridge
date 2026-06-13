"""pytest 共通フィクスチャ。

実機 (ttyHS1) は無いが pyserial 自体は CI に入れる（serial_io が
トップレベル import するため）。SerialBus インスタンスは生成せず、
rt.serial_bus に FakeSerialBus を差し込んでテストする。
rs274ngc (gcode) は CI に無く、canon.py が RS274_AVAILABLE=False に縮退する。
グローバル machine はテストごとに fresh_machine で初期化する。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def fresh_machine():
    """各テスト前に MachineState を初期化して返す。

    gateway.state.machine はモジュールグローバルなので、
    属性をデフォルトに戻す（再代入ではなく in-place リセット）。
    """
    from gateway.state import machine, MachineState
    pristine = MachineState()
    with machine.lock:
        for k, v in pristine.__dict__.items():
            if k == "lock":
                continue
            setattr(machine, k, v)
    return machine


class FakeSerialBus:
    """SerialBus の代替。送ったコマンドを記録し、固定応答を返す。"""
    def __init__(self):
        self.sent: list[str] = []      # send_cmd で送られた行
        self.jogs: list[str] = []      # send_jog で送られた行
        self.rt:   list[bytes] = []    # send_rt で送られたバイト
        self.responses: dict[str, str] = {}  # cmd -> 応答 (既定 "ok")

    def send_cmd(self, cmd: str) -> str:
        self.sent.append(cmd)
        return self.responses.get(cmd, "ok")

    def send_jog(self, cmd: str):
        self.jogs.append(cmd)

    def send_rt(self, byte: bytes):
        self.rt.append(byte)


@pytest.fixture
def fake_serial():
    """rt.serial_bus に FakeSerialBus を差し込み、テスト後に元へ戻す。"""
    from gateway import runtime as rt
    prev = rt.serial_bus
    bus = FakeSerialBus()
    rt.serial_bus = bus
    yield bus
    rt.serial_bus = prev


@pytest.fixture
def no_serial():
    """rt.serial_bus を None にする（シリアル未接続パスの検証）。"""
    from gateway import runtime as rt
    prev = rt.serial_bus
    rt.serial_bus = None
    yield
    rt.serial_bus = prev
