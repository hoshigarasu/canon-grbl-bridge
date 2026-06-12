"""スレッド間共有の機械状態。"""
import asyncio
import threading
import time
from typing import Optional

from gateway.config import INTERP_IDLE, TASK_MODE_MANUAL


class MachineState:
    """スレッド間共有の機械状態。ロックで保護。"""
    def __init__(self):
        self.lock = threading.Lock()
        # grblHAL ? レスポンスから更新
        self.grbl_state  = "Disconnected"  # Idle / Run / Hold / Alarm / ...
        self.mpos        = [0.0, 0.0, 0.0]
        self.wpos        = [0.0, 0.0, 0.0]
        self.wco         = [0.0, 0.0, 0.0]
        self.feed        = 0.0
        self.spindle_rpm = 0.0
        # gateway 管理
        self.active_file : Optional[str] = None
        self.task_mode   = TASK_MODE_MANUAL
        self.interp_state = INTERP_IDLE
        self.feed_override = 1.0
        self.estop       = False
        self.enabled     = False
        self.armed       = False
        self.flood       = False
        self.mist        = False
        self.spindle_direction = 0
        self.probe_tripped = False
        self.wcs_p       = 1  # 現在のWCS: G54=1, G55=2, ..., G59=6
        self.last_error: Optional[dict] = None  # 最後のgrblHAL拒否（error/ALARM/timeout）
        # ステップ実行用
        self.step_commands: list[str] = []
        self.step_index: int = 0
        # 実行スレッド管理
        self.run_task: Optional[asyncio.Task] = None

    def to_status_data(self) -> dict:
        with self.lock:
            return {
                "ts":               time.time(),
                "armed":            self.armed,
                "estop":            self.estop,
                "enabled":          self.enabled,
                "homed":            True,           # grblHAL $22=0 ホーミング無効→常にhomed扱い
                "task_mode":        self.task_mode,
                "interp_state":     self.interp_state,
                "state":            1,
                "machine_pos":      list(self.mpos),
                "work_pos":         list(self.wpos),
                "eoffset_z":        0.0,
                "joint_pos":        list(self.mpos),
                "g5x_offset":       [0.0, 0.0, 0.0],
                "g92_offset":       [0.0, 0.0, 0.0],
                "tool_offset":      [0.0, 0.0, 0.0],
                "dtg":              [0.0, 0.0, 0.0],
                "feed_override":    self.feed_override,
                "spindle_override":  1.0,
                "spindle_speed":     self.spindle_rpm,
                "spindle_speed_actual": self.spindle_rpm,
                "spindle_direction": self.spindle_direction,
                "rapid_override":    1.0,
                "max_velocity":      5000.0,
                "current_vel":       self.feed,
                "active_file":       self.active_file or "",
                "motion_line":       0,
                "tool_number":       0,
                "tool_diameter":     0.0,
                "tool_length":       0.0,
                "flood":             self.flood,
                "mist":              self.mist,
                "probe_tripped":     self.probe_tripped,
                "probing":           False,
                "probed_position":   [0.0, 0.0, 0.0],
                "last_error":        self.last_error,
            }


machine = MachineState()
