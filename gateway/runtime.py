"""起動時に再代入される共有シングルトン。

`from gateway import runtime as rt` でimportし、必ず `rt.serial_bus` の
属性アクセスで参照すること (from-importすると起動前のNoneが束縛される)。
"""
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from gateway.serial_io import SerialBus

serial_bus: Optional["SerialBus"] = None          # app.lifespan で初期化
event_loop: Optional["asyncio.AbstractEventLoop"] = None  # app.lifespan でキャプチャ
