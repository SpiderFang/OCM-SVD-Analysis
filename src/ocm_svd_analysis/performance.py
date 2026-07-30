"""提供 SVD 計算與重繪流程共用的單調時鐘效能紀錄。

本模組只量測 wall-clock elapsed time，不量測 CPU time。這是刻意的選擇：六區同時執行
時，研究團隊真正需要區分的是每區等待 memory-map I/O、執行 BLAS 與輸出圖檔所占的實際
時間，而不是把多執行緒 CPU 秒數相加。`time.perf_counter()` 不受系統時鐘校時影響，
適合比較同一個 run 內各階段，但不同主機間仍應搭配硬體與平行設定一起解讀。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class PerformanceRecorder:
    """累積互不重疊的流程階段 wall time，並產生可寫入 metadata 的摘要。

    `stage_seconds` 只接受每個名稱一次，避免程式重構後不小心把兩段不同工作合併成同一
    數字。所有值使用秒與浮點數保存；其精度足以比較 I/O、SVD 與繪圖，不能解讀成作業
    系統排程或硬體計數器等級的 benchmark。
    """

    started_at: float = field(default_factory=time.perf_counter)
    stage_seconds: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def measure(self, stage_name: str) -> Iterator[None]:
        """量測一個具名階段，無論成功或拋出例外都結束該段計時。

        失敗流程不會發布正式 metadata，但在呼叫端攔截例外做除錯時仍可檢查 recorder。
        同名階段被再次使用通常代表計時邊界定義不清，因此直接拒絕而不偷偷累加。
        """

        if not stage_name or stage_name in self.stage_seconds:
            raise ValueError(f"效能階段名稱必須非空白且不可重複: {stage_name!r}")
        stage_started_at = time.perf_counter()
        try:
            yield
        finally:
            self.stage_seconds[stage_name] = time.perf_counter() - stage_started_at

    def to_metadata(self, *, scope_end: str) -> dict[str, object]:
        """建立可序列化效能摘要，明載計時範圍與未分段的框架開銷。

        `scope_end` 必須說明總時間在哪個發布步驟前截止。正式 run 的 metadata 位於要被
        原子 rename 的目錄內，因此不可能在不破壞 immutable 契約的情況下，於 rename
        完成後再回寫自身；六區 batch 的外層 elapsed time 會另外包含最後寫檔與 rename。
        """

        total_seconds = time.perf_counter() - self.started_at
        measured_stage_seconds = float(sum(self.stage_seconds.values()))
        return {
            "clock": "time.perf_counter monotonic wall clock",
            "unit": "seconds",
            "scope_end": scope_end,
            "stages_seconds": {name: float(value) for name, value in self.stage_seconds.items()},
            "measured_stage_sum_seconds": measured_stage_seconds,
            "unattributed_framework_overhead_seconds": max(0.0, float(total_seconds - measured_stage_seconds)),
            "total_seconds": float(total_seconds),
        }
