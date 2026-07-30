"""在多核心 SERVER 平行執行多個獨立表層 SVD 分析單元。

本模組只協調已版本化的單區 SVD 設定，不接觸原始 SCHISM NetCDF，也不重寫
OCM-Data-Preprocessing 的 flow cache。正式 SERVER 優先把每個區域交給獨立 process，
可避免 matplotlib、NumPy 與每個區域的暫存輸出互相共用可變狀態；只有 process semaphore
被受限 sandbox 禁用時才退回 thread。單區內的月檔 I/O 與 BLAS 執行緒仍由
`surface_multivariate_svd` 的設定及 threadpoolctl 管控。

六區 2025 批次的設計基準是至少 32 個已配置 CPU 核心：最多六區同時執行、每區四個
BLAS 執行緒，因此密集線性代數最多使用 24 核，留下核心給作業系統、網路檔案系統與
短暫重疊的 memory-map I/O。這是受控平行化，而不是讓每個 process 任意吃滿 CPU。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .surface_multivariate_svd import AnalysisConfig, load_analysis_config, run_surface_multivariate_svd


BATCH_SCHEMA_VERSION = "1.0.0"
"""多區 SVD 批次設定版本，與單區設定和上游 flow cache schema 分開管理。"""


@dataclass(frozen=True)
class BatchRegion:
    """一個要由批次協調器啟動的 SVD 分析單元。

    `config_path` 在載入時已相對於 batch JSON 解析為絕對路徑，避免 SERVER 以不同目前
    工作目錄執行時讀到錯誤設定；`analysis_config` 保存驗證結果，用來檢查單區資源上限
    與 OCM-Data-Preprocessing 的分析單元版本是否和批次契約一致。
    """

    analysis_unit_id: str
    config_path: Path
    analysis_config: AnalysisConfig


@dataclass(frozen=True)
class BatchConfig:
    """經驗證的多區 SVD 執行計畫與 SERVER 核心配置限制。"""

    batch_label: str
    source_analysis_units_config: str
    source_analysis_units_config_sha256: str
    server_minimum_cpu_cores: int
    max_concurrent_regions: int
    per_region_linear_algebra_threads: int
    per_region_io_workers: int
    regions: tuple[BatchRegion, ...]


@dataclass(frozen=True)
class BatchRegionResult:
    """單一子 process 的安全回傳值；不攜帶大型陣列，只回報成果與完整 wall time。

    `elapsed_seconds` 從子 process 進入單區高階函式前開始，到該函式完成 metadata 寫入與
    原子 rename 後停止，因此比單區 metadata 內的 `performance.total_seconds` 範圍稍廣。
    六區報告應以這個欄位比較每區實際完成時間，再用各 run 的 stages 追查慢在哪一段。
    """

    analysis_unit_id: str
    config_path: Path
    status: str
    result_dir: Path | None
    elapsed_seconds: float


@dataclass(frozen=True)
class BatchExecutionResult:
    """完整批次的排序後成果、實際同時區域數與端到端 wall time。"""

    batch_label: str
    visible_cpu_cores: int
    concurrent_regions_used: int
    execution_backend: str
    regions: tuple[BatchRegionResult, ...]
    total_elapsed_seconds: float


def _require(condition: bool, message: str) -> None:
    """在批次資源或可追溯性契約不成立時停止，避免半套設定被誤送到 SERVER。"""

    if not condition:
        raise ValueError(message)


def _read_json_object(path: Path) -> dict[str, Any]:
    """讀取 batch JSON object，明確拒絕不存在、空白或清單根節點的設定檔。"""

    if not path.is_file():
        raise FileNotFoundError(f"找不到 SVD batch 設定檔: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"SVD batch JSON 根節點必須是物件: {path}")
    return payload


def _positive_int(value: object, field: str) -> int:
    """驗證核心、worker 與區域數是正整數，防止 bool 或 0 造成無聲退化為序列執行。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必須是正整數")
    return value


def _sha256_string(value: object, field: str) -> str:
    """驗證設定雜湊為小寫 SHA-256，使下游可辨識分析區域定義是否被悄悄變更。"""

    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} 必須是小寫 64 字元 SHA-256")
    return value


def load_batch_config(batch_config_path: Path) -> BatchConfig:
    """讀取、交叉驗證六區 SVD batch 與各單區設定。

    除了 JSON 結構，本函式強制每個單區都宣告相同的上游分析單元設定雜湊、同一組每區
    I/O／BLAS 限制與相符的 analysis unit ID。這能避免有人只修改某個 bbox JSON 後仍
    以舊 batch 混跑，導致六個成果看似可比較、實際上地理邊界版本不同。
    """

    resolved_path = batch_config_path.resolve()
    raw = _read_json_object(resolved_path)
    _require(raw.get("schema_version") == BATCH_SCHEMA_VERSION, f"batch schema_version 必須是 {BATCH_SCHEMA_VERSION}")
    _require(raw.get("analysis_kind") == "surface_multivariate_svd_batch", "batch analysis_kind 必須是 surface_multivariate_svd_batch")
    batch_label = raw.get("batch_label")
    if not isinstance(batch_label, str) or not batch_label.strip():
        raise ValueError("batch_label 必須是非空白文字")
    source_config = raw.get("source_analysis_units_config")
    if not isinstance(source_config, str) or not source_config.strip():
        raise ValueError("source_analysis_units_config 必須是非空白文字")
    source_hash = _sha256_string(raw.get("source_analysis_units_config_sha256"), "source_analysis_units_config_sha256")

    parallel = raw.get("parallel_execution")
    if not isinstance(parallel, dict):
        raise ValueError("parallel_execution 必須是物件")
    server_minimum_cpu_cores = _positive_int(parallel.get("server_minimum_cpu_cores"), "parallel_execution.server_minimum_cpu_cores")
    max_concurrent_regions = _positive_int(parallel.get("max_concurrent_regions"), "parallel_execution.max_concurrent_regions")
    per_region_threads = _positive_int(parallel.get("per_region_linear_algebra_threads"), "parallel_execution.per_region_linear_algebra_threads")
    per_region_io_workers = _positive_int(parallel.get("per_region_io_workers"), "parallel_execution.per_region_io_workers")
    _require(
        max_concurrent_regions * per_region_threads <= server_minimum_cpu_cores,
        "batch 的同時區域數 × 每區 BLAS 執行緒不得超過宣告的 SERVER 最小核心數",
    )

    regions_raw = raw.get("region_configs")
    if not isinstance(regions_raw, list) or not regions_raw:
        raise ValueError("region_configs 必須是非空白清單")
    regions: list[BatchRegion] = []
    seen_units: set[str] = set()
    seen_config_paths: set[Path] = set()
    for index, entry in enumerate(regions_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"region_configs[{index}] 必須是物件")
        analysis_unit_id = entry.get("analysis_unit_id")
        relative_config = entry.get("config")
        if not isinstance(analysis_unit_id, str) or not analysis_unit_id.strip():
            raise ValueError(f"region_configs[{index}].analysis_unit_id 必須是非空白文字")
        if not isinstance(relative_config, str) or not relative_config.strip():
            raise ValueError(f"region_configs[{index}].config 必須是非空白文字")
        _require(analysis_unit_id not in seen_units, f"batch analysis_unit_id 重複: {analysis_unit_id}")
        config_path = (resolved_path.parent / relative_config).resolve()
        _require(config_path not in seen_config_paths, f"batch 重複引用同一單區設定: {config_path}")
        analysis_config = load_analysis_config(config_path)
        _require(analysis_config.analysis_unit_id == analysis_unit_id, f"{config_path.name} 的 analysis_unit_id 與 batch 不一致")
        _require(analysis_config.source_analysis_units_config == source_config, f"{config_path.name} 的 source_analysis_units_config 與 batch 不一致")
        _require(analysis_config.source_analysis_units_config_sha256 == source_hash, f"{config_path.name} 的上游分析單元 SHA-256 與 batch 不一致")
        _require(analysis_config.linear_algebra_threads == per_region_threads, f"{config_path.name} 的 linear_algebra_threads 必須是 {per_region_threads}")
        _require(analysis_config.io_workers == per_region_io_workers, f"{config_path.name} 的 io_workers 必須是 {per_region_io_workers}")
        seen_units.add(analysis_unit_id)
        seen_config_paths.add(config_path)
        regions.append(BatchRegion(analysis_unit_id, config_path, analysis_config))
    _require(len(regions) <= max_concurrent_regions, "本版 batch 要求 max_concurrent_regions 至少涵蓋列出的所有區域；請調高設定或拆成明確的獨立 batch")
    return BatchConfig(
        batch_label=batch_label,
        source_analysis_units_config=source_config,
        source_analysis_units_config_sha256=source_hash,
        server_minimum_cpu_cores=server_minimum_cpu_cores,
        max_concurrent_regions=max_concurrent_regions,
        per_region_linear_algebra_threads=per_region_threads,
        per_region_io_workers=per_region_io_workers,
        regions=tuple(regions),
    )


def _run_one_region(
    region: BatchRegion,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool,
    allow_trial: bool,
    make_figures: bool,
    skip_existing: bool,
) -> BatchRegionResult:
    """在獨立 process 執行一區 SVD，保留單區原子發布與拒絕覆寫語意。

    若操作者明確選擇 `skip_existing`，只把已存在的 immutable run 視為可重用成果；預設
    仍傳遞 `FileExistsError`，防止意外把舊資料或舊設定混入一次新的科學批次。
    """

    region_started_at = time.perf_counter()
    try:
        result_dir = run_surface_multivariate_svd(
            config_path=region.config_path,
            surface_root=surface_root,
            output_root=output_root,
            allow_partial_months=allow_partial_months,
            allow_trial=allow_trial,
            make_figures=make_figures,
        )
    except FileExistsError:
        if not skip_existing:
            raise
        return BatchRegionResult(
            region.analysis_unit_id,
            region.config_path,
            "already_exists",
            None,
            time.perf_counter() - region_started_at,
        )
    return BatchRegionResult(
        region.analysis_unit_id,
        region.config_path,
        "created",
        result_dir,
        time.perf_counter() - region_started_at,
    )


def run_surface_multivariate_svd_batch(
    *,
    batch_config_path: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
    make_figures: bool = True,
    skip_existing: bool = False,
) -> BatchExecutionResult:
    """以受控平行完成一份已驗證 batch 的所有 SVD 區域。

    實際同時區域數依可見 CPU 自動收斂為 `floor(cpu / per_region_blas_threads)`，不會因
    SLURM/PBS 只配到部分核心仍強行超額配置。若 SERVER 有 32 核且使用六區 v1 設定，
    則會同時跑六區、每區四個 BLAS 核；若任何區域失敗，尚未開始的 future 會取消，已
    原子發布的成功成果會保留供檢查，絕不由協調器刪除。
    """

    batch_started_at = time.perf_counter()
    config = load_batch_config(batch_config_path)
    resolved_surface_root = surface_root.resolve()
    resolved_output_root = output_root.resolve()
    _require(resolved_surface_root.is_dir(), f"surface_root 不存在或不是目錄: {resolved_surface_root}")
    visible_cpu_cores = os.cpu_count() or 1
    cpu_limited_regions = max(1, visible_cpu_cores // config.per_region_linear_algebra_threads)
    worker_count = min(config.max_concurrent_regions, len(config.regions), cpu_limited_regions)

    futures: dict[Future[BatchRegionResult], BatchRegion] = {}
    results_by_unit: dict[str, BatchRegionResult] = {}
    # 正式 SERVER 優先使用 process，隔離各區 matplotlib 全域設定、NumPy 記憶體與暫存
    # 目錄。少數受限桌面 sandbox 會禁止 Python 建立 process semaphore；只有在 executor
    # 初始化階段才退回 thread，以保留月檔 I/O 與會釋放 GIL 的 NumPy/BLAS 平行測試能力。
    # process 已啟動後的區域失敗絕不重跑，避免與可能已發布的 immutable run 衝突。
    try:
        executor: ProcessPoolExecutor | ThreadPoolExecutor = ProcessPoolExecutor(max_workers=worker_count)
        execution_backend = "process"
    except (OSError, PermissionError):
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocm-svd-region")
        execution_backend = "thread_fallback_process_unavailable"
    with executor:
        for region in config.regions:
            future = executor.submit(
                _run_one_region,
                region,
                resolved_surface_root,
                resolved_output_root,
                allow_partial_months,
                allow_trial,
                make_figures,
                skip_existing,
            )
            futures[future] = region
        for future in as_completed(futures):
            region = futures[future]
            try:
                results_by_unit[region.analysis_unit_id] = future.result()
            except Exception as error:
                for pending_future in futures:
                    pending_future.cancel()
                raise RuntimeError(f"SVD batch 在 {region.analysis_unit_id} 失敗；已取消尚未開始的區域，已完成成果仍保留") from error
    ordered_results = tuple(results_by_unit[region.analysis_unit_id] for region in config.regions)
    return BatchExecutionResult(
        config.batch_label,
        visible_cpu_cores,
        worker_count,
        execution_backend,
        ordered_results,
        time.perf_counter() - batch_started_at,
    )
