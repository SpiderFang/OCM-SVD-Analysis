"""平行重繪多個既有 SVD run，供六區報告一次產生一致圖層。

每區使用獨立 process，避免 Matplotlib 全域狀態與大量 raster buffer 互相干擾。這個
batch 不讀 surface cache、不執行 SVD；它只協調 `replot_surface_multivariate_svd`
建立各自 immutable figure bundle。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .replot import replot_surface_multivariate_svd


@dataclass(frozen=True)
class ReplotBatchItem:
    """一個經驗證、準備平行重繪的來源 run 與可選完整設定。"""

    run_id: str
    run_dir: Path
    config_path: Path | None


@dataclass(frozen=True)
class ReplotBatchItemResult:
    """單區重繪子程序的 figure bundle 路徑與完整 wall time。"""

    run_id: str
    bundle_dir: Path
    elapsed_seconds: float


@dataclass(frozen=True)
class ReplotBatchExecutionResult:
    """多區重繪的排序成果、實際 backend、平行數與端到端 wall time。"""

    visible_cpu_cores: int
    concurrent_regions_used: int
    execution_backend: str
    items: tuple[ReplotBatchItemResult, ...]
    total_elapsed_seconds: float


def _load_run_id(run_dir: Path) -> str:
    """只讀來源 metadata 取得 run ID，並確認目錄名稱未與內容脫鉤。"""

    metadata_path = run_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"六區重繪來源缺少 metadata.json: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError(f"六區重繪來源 metadata 根節點必須是物件: {metadata_path}")
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        raise ValueError(f"六區重繪來源 run_id 與目錄名稱不一致: {run_dir}")
    return run_id


def _replot_one_item(item: ReplotBatchItem, output_root: Path) -> ReplotBatchItemResult:
    """在單一 worker 重繪一區，回傳不含大型陣列的安全結果。"""

    started_at = time.perf_counter()
    bundle_dir = replot_surface_multivariate_svd(
        run_dir=item.run_dir,
        output_root=output_root,
        config_path=item.config_path,
    )
    return ReplotBatchItemResult(item.run_id, bundle_dir, time.perf_counter() - started_at)


def replot_surface_multivariate_svd_batch(
    *,
    run_dirs: tuple[Path, ...],
    output_root: Path,
    max_concurrent_regions: int = 6,
    config_paths: tuple[Path, ...] | None = None,
) -> ReplotBatchExecutionResult:
    """以最多六個獨立 process 同時建立多區 figure bundles。

    `config_paths` 若提供，數量與順序必須逐一對應 `run_dirs`；每份設定仍由單區重繪器
    強制檢查只有 `figures` 可不同。省略時直接使用每個來源 run 保存的 `config.json`。
    任一區失敗會取消尚未開始的工作，已原子發布的 bundle 保留供檢查，不會自動刪除。
    """

    batch_started_at = time.perf_counter()
    if isinstance(max_concurrent_regions, bool) or not isinstance(max_concurrent_regions, int) or max_concurrent_regions < 1:
        raise ValueError("max_concurrent_regions 必須是正整數")
    if not run_dirs:
        raise ValueError("六區重繪至少需要一個 run_dir")
    if config_paths is not None and len(config_paths) != len(run_dirs):
        raise ValueError("config_paths 若提供，數量必須與 run_dirs 完全一致")

    items: list[ReplotBatchItem] = []
    seen_run_ids: set[str] = set()
    seen_run_dirs: set[Path] = set()
    for index, run_dir in enumerate(run_dirs):
        resolved_run_dir = run_dir.resolve()
        if not resolved_run_dir.is_dir():
            raise FileNotFoundError(f"六區重繪來源目錄不存在: {resolved_run_dir}")
        if resolved_run_dir in seen_run_dirs:
            raise ValueError(f"六區重繪不可重複提交同一 run 目錄: {resolved_run_dir}")
        run_id = _load_run_id(resolved_run_dir)
        if run_id in seen_run_ids:
            raise ValueError(f"六區重繪 run_id 重複: {run_id}")
        resolved_config = config_paths[index].resolve() if config_paths is not None else None
        items.append(ReplotBatchItem(run_id, resolved_run_dir, resolved_config))
        seen_run_dirs.add(resolved_run_dir)
        seen_run_ids.add(run_id)

    resolved_output_root = output_root.resolve()
    visible_cpu_cores = os.cpu_count() or 1
    worker_count = min(max_concurrent_regions, len(items), visible_cpu_cores)
    futures: dict[Future[ReplotBatchItemResult], ReplotBatchItem] = {}
    results_by_run_id: dict[str, ReplotBatchItemResult] = {}

    # 正式 SERVER 使用 process 隔離 Matplotlib；受限本機環境若無法建立 semaphore，才退回
    # thread。重繪沒有 BLAS 密集區段，worker 上限直接依區域數與可見 CPU 收斂。
    try:
        executor: ProcessPoolExecutor | ThreadPoolExecutor = ProcessPoolExecutor(max_workers=worker_count)
        execution_backend = "process"
    except (OSError, PermissionError):
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocm-svd-replot")
        execution_backend = "thread_fallback_process_unavailable"
    with executor:
        for item in items:
            future = executor.submit(_replot_one_item, item, resolved_output_root)
            futures[future] = item
        for future in as_completed(futures):
            item = futures[future]
            try:
                results_by_run_id[item.run_id] = future.result()
            except Exception as error:
                for pending_future in futures:
                    pending_future.cancel()
                raise RuntimeError(f"figure bundle batch 在 {item.run_id} 失敗；已取消尚未開始的區域") from error

    ordered_results = tuple(results_by_run_id[item.run_id] for item in items)
    return ReplotBatchExecutionResult(
        visible_cpu_cores=visible_cpu_cores,
        concurrent_regions_used=worker_count,
        execution_backend=execution_backend,
        items=ordered_results,
        total_elapsed_seconds=time.perf_counter() - batch_started_at,
    )
