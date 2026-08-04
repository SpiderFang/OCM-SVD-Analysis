"""以分組依序執行、跨資料區域隔離方式建立多區固定深度 SVD family。

此模組只協調 ``fixed_depth_multivariate_svd`` 設定，輸入同時是 paired
``ocm_native`` 與 ``ocm_surface`` 快取。它刻意不共用表層 ``batch.py`` 的設定型別或
輸出目錄：固定深度科學成果永遠發布至 ``fixed_depth_svd/``，圖面若另行建立則永遠發布
至 ``fixed_depth_svd_figure_bundles/``。這個命名空間隔離避免操作人員把完整表層 SVD 與
同遮罩垂向比較 family 誤認為可互相覆寫的同一產品。

分組依序執行的核心目的不是增加 SVD 求解核心數；固定深度的大部分成本在 native
``hvel/zcor`` 的分散 memory-map 讀取。batch JSON 因此明確列出每個「執行組」，並拒絕
把同一資料區域放進同一組，以降低共享 NFS 同時隨機索引而拖慢或耗盡記憶體的風險。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fixed_depth_multivariate_svd import (
    FIXED_DEPTH_ANALYSIS_KIND,
    FixedDepthConfig,
    load_fixed_depth_config,
    run_fixed_depth_multivariate_svd,
)
from .surface_multivariate_svd import _require


FIXED_DEPTH_BATCH_SCHEMA_VERSION = "1.0.0"
"""固定深度「分組依序執行」batch JSON 的版本；不可與表層 batch schema 混用。"""

FIXED_DEPTH_RESULT_NAMESPACE = "fixed_depth_svd"
"""固定深度科學 family 唯一允許的輸出父目錄名稱。"""

FIXED_DEPTH_FIGURE_NAMESPACE = "fixed_depth_svd_figure_bundles"
"""固定深度圖包唯一允許的輸出父目錄名稱。"""


@dataclass(frozen=True)
class FixedDepthBatchRegion:
    """一個已驗證、可由獨立 process 執行的固定深度分析區。

    ``config_path`` 一律在讀取 batch JSON 時轉成絕對路徑，避免 SERVER 從不同工作目錄
    執行時誤讀表層設定。``config`` 保留已驗證的固定深度 family 契約，供執行組檢查
    資料區域、I/O worker 與 BLAS 上限。
    """

    analysis_unit_id: str
    config_path: Path
    config: FixedDepthConfig


@dataclass(frozen=True)
class FixedDepthExecutionGroup:
    """可同時執行、但與其他組依序進行的一組固定深度區域。

    同一執行組內的各區必須使用不同 ``flow_domain_id``；例如龜山與貢寮共用東北臺灣
    native cache，必須放在不同執行組。這是 I/O 安全契約，不能由 CLI 臨時覆寫。
    """

    execution_group_id: str
    regions: tuple[FixedDepthBatchRegion, ...]


@dataclass(frozen=True)
class FixedDepthBatchConfig:
    """經交叉驗證的固定深度批次計畫與其獨立輸出命名空間。"""

    batch_label: str
    source_analysis_units_config: str
    source_analysis_units_config_sha256: str
    server_minimum_cpu_cores: int
    max_concurrent_regions: int
    per_region_linear_algebra_threads: int
    per_region_io_workers: int
    result_namespace: str
    figure_bundle_namespace: str
    execution_groups: tuple[FixedDepthExecutionGroup, ...]


@dataclass(frozen=True)
class FixedDepthBatchRegionResult:
    """單區固定深度 family 的完成狀態與端到端牆鐘時間。

    子 process 只回傳小型 provenance 資訊，絕不將大型 SVD 陣列傳回父 process；成果在
    子 process 成功完成後已原子發布，失敗時則由單區 runner 清理其 partial 目錄。
    """

    execution_group_id: str
    analysis_unit_id: str
    status: str
    result_dir: Path | None
    elapsed_seconds: float


@dataclass(frozen=True)
class FixedDepthBatchExecutionResult:
    """整份固定深度「分組依序執行」batch 的可日誌化摘要。"""

    batch_label: str
    result_namespace: str
    figure_bundle_namespace: str
    visible_cpu_cores: int
    execution_backend: str
    regions: tuple[FixedDepthBatchRegionResult, ...]
    total_elapsed_seconds: float


def _read_json_object(path: Path) -> dict[str, Any]:
    """讀取 batch JSON object，拒絕遺失或非 object 設定以避免隱性預設值。"""

    if not path.is_file():
        raise FileNotFoundError(f"找不到固定深度 batch 設定檔: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"固定深度 batch JSON 根節點必須是物件: {path}")
    return payload


def _positive_int(value: object, field: str) -> int:
    """驗證資源上限是正整數，避免 bool、零或負值無聲改變排程。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必須是正整數")
    return value


def _sha256_string(value: object, field: str) -> str:
    """驗證上游分析單元定義雜湊，保證六區 AOI 來自同一版本。"""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} 必須是小寫 64 字元 SHA-256")
    return value


def load_fixed_depth_batch_config(batch_config_path: Path) -> FixedDepthBatchConfig:
    """讀取並嚴格驗證六區固定深度「分組依序執行」batch。

    本函式除了確認單區均為 fixed-depth analysis kind，也會鎖定兩個獨立輸出命名空間、
    六區的上游 AOI 雜湊、每區資源上限與執行組資料區域互斥。因而即使人員誤把表層 config
    填入 batch，或試圖把北竿與南竿同時讀連江 native cache，流程也會在任何大型 I/O 前
    停止。
    """

    resolved_path = batch_config_path.resolve()
    raw = _read_json_object(resolved_path)
    _require(
        raw.get("schema_version") == FIXED_DEPTH_BATCH_SCHEMA_VERSION,
        f"固定深度 batch schema_version 必須是 {FIXED_DEPTH_BATCH_SCHEMA_VERSION}",
    )
    _require(
        raw.get("analysis_kind") == "fixed_depth_multivariate_svd_batch",
        "固定深度 batch analysis_kind 必須是 fixed_depth_multivariate_svd_batch",
    )
    batch_label = raw.get("batch_label")
    if not isinstance(batch_label, str) or not batch_label.strip():
        raise ValueError("batch_label 必須是非空白文字")
    source_config = raw.get("source_analysis_units_config")
    if not isinstance(source_config, str) or not source_config.strip():
        raise ValueError("source_analysis_units_config 必須是非空白文字")
    source_hash = _sha256_string(
        raw.get("source_analysis_units_config_sha256"),
        "source_analysis_units_config_sha256",
    )
    _require(
        raw.get("result_namespace") == FIXED_DEPTH_RESULT_NAMESPACE,
        "固定深度 batch result_namespace 必須是 fixed_depth_svd，禁止指向表層 svd/",
    )
    _require(
        raw.get("figure_bundle_namespace") == FIXED_DEPTH_FIGURE_NAMESPACE,
        "固定深度 batch figure_bundle_namespace 必須是 fixed_depth_svd_figure_bundles，禁止指向表層圖包",
    )

    parallel = raw.get("parallel_execution")
    if not isinstance(parallel, dict):
        raise ValueError("parallel_execution 必須是物件")
    server_minimum_cpu_cores = _positive_int(
        parallel.get("server_minimum_cpu_cores"),
        "parallel_execution.server_minimum_cpu_cores",
    )
    max_concurrent_regions = _positive_int(
        parallel.get("max_concurrent_regions"),
        "parallel_execution.max_concurrent_regions",
    )
    per_region_threads = _positive_int(
        parallel.get("per_region_linear_algebra_threads"),
        "parallel_execution.per_region_linear_algebra_threads",
    )
    per_region_io_workers = _positive_int(
        parallel.get("per_region_io_workers"),
        "parallel_execution.per_region_io_workers",
    )
    _require(
        max_concurrent_regions * per_region_threads <= server_minimum_cpu_cores,
        "固定深度 batch 的同時區域數 × 每區 BLAS 執行緒不得超過最小 CPU 核心數",
    )

    region_entries = raw.get("region_configs")
    if not isinstance(region_entries, list) or not region_entries:
        raise ValueError("region_configs 必須是非空白清單")
    regions: dict[str, FixedDepthBatchRegion] = {}
    seen_config_paths: set[Path] = set()
    for index, entry in enumerate(region_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"region_configs[{index}] 必須是物件")
        analysis_unit_id = entry.get("analysis_unit_id")
        relative_config = entry.get("config")
        if not isinstance(analysis_unit_id, str) or not analysis_unit_id.strip():
            raise ValueError(f"region_configs[{index}].analysis_unit_id 必須是非空白文字")
        if not isinstance(relative_config, str) or not relative_config.strip():
            raise ValueError(f"region_configs[{index}].config 必須是非空白文字")
        _require(
            analysis_unit_id not in regions,
            f"固定深度 batch analysis_unit_id 重複: {analysis_unit_id}",
        )
        config_path = (resolved_path.parent / relative_config).resolve()
        _require(
            config_path not in seen_config_paths,
            f"固定深度 batch 重複引用同一設定: {config_path}",
        )
        config = load_fixed_depth_config(config_path)
        _require(
            config.base.raw.get("analysis_kind") == FIXED_DEPTH_ANALYSIS_KIND,
            f"{config_path.name} 不是固定深度 SVD 設定",
        )
        _require(
            config.base.analysis_unit_id == analysis_unit_id,
            f"{config_path.name} 的 analysis_unit_id 與 batch 不一致",
        )
        _require(
            config.base.source_analysis_units_config == source_config,
            f"{config_path.name} 的 source_analysis_units_config 與 batch 不一致",
        )
        _require(
            config.base.source_analysis_units_config_sha256 == source_hash,
            f"{config_path.name} 的上游分析單元 SHA-256 與 batch 不一致",
        )
        _require(
            config.base.io_workers == per_region_io_workers,
            f"{config_path.name} 的 io_workers 必須是 {per_region_io_workers}",
        )
        _require(
            config.base.linear_algebra_threads == per_region_threads,
            f"{config_path.name} 的 linear_algebra_threads 必須是 {per_region_threads}",
        )
        regions[analysis_unit_id] = FixedDepthBatchRegion(
            analysis_unit_id=analysis_unit_id,
            config_path=config_path,
            config=config,
        )
        seen_config_paths.add(config_path)

    group_entries = raw.get("execution_groups")
    if not isinstance(group_entries, list) or not group_entries:
        raise ValueError("execution_groups 必須是非空白清單")
    execution_groups: list[FixedDepthExecutionGroup] = []
    scheduled_units: set[str] = set()
    seen_group_ids: set[str] = set()
    for index, entry in enumerate(group_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"execution_groups[{index}] 必須是物件")
        execution_group_id = entry.get("execution_group_id")
        unit_ids = entry.get("analysis_unit_ids")
        if not isinstance(execution_group_id, str) or not execution_group_id.strip():
            raise ValueError(f"execution_groups[{index}].execution_group_id 必須是非空白文字")
        _require(
            execution_group_id not in seen_group_ids,
            f"固定深度 batch execution_group_id 重複: {execution_group_id}",
        )
        if not isinstance(unit_ids, list) or not unit_ids:
            raise ValueError(f"execution_groups[{index}].analysis_unit_ids 必須是非空白清單")
        _require(
            len(unit_ids) <= max_concurrent_regions,
            f"執行組 {execution_group_id} 的區域數超過 max_concurrent_regions",
        )
        group_regions: list[FixedDepthBatchRegion] = []
        group_domains: set[str] = set()
        for unit_id in unit_ids:
            if not isinstance(unit_id, str):
                raise ValueError(f"執行組 {execution_group_id} 的 analysis_unit_id 必須是字串")
            _require(unit_id in regions, f"執行組 {execution_group_id} 引用未知區域: {unit_id}")
            _require(
                unit_id not in scheduled_units,
                f"固定深度區域不能出現在兩個執行組: {unit_id}",
            )
            region = regions[unit_id]
            domain = region.config.base.domain_id
            _require(
                domain not in group_domains,
                f"執行組 {execution_group_id} 同時讀取重複資料區域 {domain}；請分到不同執行組",
            )
            group_regions.append(region)
            group_domains.add(domain)
            scheduled_units.add(unit_id)
        execution_groups.append(
            FixedDepthExecutionGroup(
                execution_group_id=execution_group_id,
                regions=tuple(group_regions),
            )
        )
        seen_group_ids.add(execution_group_id)
    _require(
        scheduled_units == set(regions),
        "每一個固定深度區域必須且只能出現在一個 execution group",
    )

    return FixedDepthBatchConfig(
        batch_label=batch_label,
        source_analysis_units_config=source_config,
        source_analysis_units_config_sha256=source_hash,
        server_minimum_cpu_cores=server_minimum_cpu_cores,
        max_concurrent_regions=max_concurrent_regions,
        per_region_linear_algebra_threads=per_region_threads,
        per_region_io_workers=per_region_io_workers,
        result_namespace=FIXED_DEPTH_RESULT_NAMESPACE,
        figure_bundle_namespace=FIXED_DEPTH_FIGURE_NAMESPACE,
        execution_groups=tuple(execution_groups),
    )


def _run_one_fixed_depth_region(
    region: FixedDepthBatchRegion,
    execution_group_id: str,
    native_root: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool,
    allow_trial: bool,
    skip_existing: bool,
) -> FixedDepthBatchRegionResult:
    """在一個獨立 process 建立單區 immutable fixed-depth family。

    ``skip_existing`` 是刻意選擇的可恢復行為：已原子發布的固定深度 run 可重用，但沒有
    任何情況會覆寫它。partial 目錄由單區 runner 的例外處理清理，故下次執行組可安全重啟。
    """

    started_at = time.perf_counter()
    try:
        result_dir = run_fixed_depth_multivariate_svd(
            config_path=region.config_path,
            native_root=native_root,
            surface_root=surface_root,
            output_root=output_root,
            allow_partial_months=allow_partial_months,
            allow_trial=allow_trial,
        )
    except FileExistsError:
        if not skip_existing:
            raise
        return FixedDepthBatchRegionResult(
            execution_group_id=execution_group_id,
            analysis_unit_id=region.analysis_unit_id,
            status="already_exists",
            result_dir=None,
            elapsed_seconds=time.perf_counter() - started_at,
        )
    return FixedDepthBatchRegionResult(
        execution_group_id=execution_group_id,
        analysis_unit_id=region.analysis_unit_id,
        status="created",
        result_dir=result_dir,
        elapsed_seconds=time.perf_counter() - started_at,
    )


def run_fixed_depth_multivariate_svd_batch(
    *,
    batch_config_path: Path,
    native_root: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
    skip_existing: bool = False,
) -> FixedDepthBatchExecutionResult:
    """依 batch 執行組順序建立獨立固定深度 family，絕不觸及表層成果。

    process executor 讓同時區域的 NumPy 記憶體、memory-map 與暫存輸出彼此隔離；少數
    受限桌面環境若無法建立 process semaphore，才退回 thread。每一執行組完成後才開始下
    一組，故 batch JSON 的共用資料區域隔離策略即使在 CPU 很多的 SERVER 也不會被繞過。
    任何區域失敗時，尚未開始的 future 會取消，後續執行組不會啟動；已發布的 family
    保持 immutable，供人工檢查或以 ``--skip-existing`` 恢復。
    """

    started_at = time.perf_counter()
    config = load_fixed_depth_batch_config(batch_config_path)
    resolved_native_root = native_root.resolve()
    resolved_surface_root = surface_root.resolve()
    resolved_output_root = output_root.resolve()
    _require(resolved_native_root.is_dir(), f"native_root 不存在或不是目錄: {resolved_native_root}")
    _require(resolved_surface_root.is_dir(), f"surface_root 不存在或不是目錄: {resolved_surface_root}")
    visible_cpu_cores = os.cpu_count() or 1
    cpu_limited_regions = max(1, visible_cpu_cores // config.per_region_linear_algebra_threads)
    all_results: list[FixedDepthBatchRegionResult] = []

    try:
        executor_type: type[ProcessPoolExecutor] | type[ThreadPoolExecutor] = ProcessPoolExecutor
        execution_backend = "process"
        # 先建立一次小型 executor，及早確認受限環境是否允許 process semaphore；實際執行組
        # executor 仍在下方各自建立，避免某個執行組的失敗留下可重用狀態給下一組。
        probe = executor_type(max_workers=1)
        probe.shutdown(wait=True)
    except (OSError, PermissionError):
        executor_type = ThreadPoolExecutor
        execution_backend = "thread_fallback_process_unavailable"

    for execution_group in config.execution_groups:
        worker_count = min(
            len(execution_group.regions),
            config.max_concurrent_regions,
            cpu_limited_regions,
        )
        futures: dict[Future[FixedDepthBatchRegionResult], FixedDepthBatchRegion] = {}
        results_by_unit: dict[str, FixedDepthBatchRegionResult] = {}
        with executor_type(max_workers=worker_count) as executor:
            for region in execution_group.regions:
                future = executor.submit(
                    _run_one_fixed_depth_region,
                    region,
                    execution_group.execution_group_id,
                    resolved_native_root,
                    resolved_surface_root,
                    resolved_output_root,
                    allow_partial_months,
                    allow_trial,
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
                    raise RuntimeError(
                        f"固定深度 batch 在執行組 {execution_group.execution_group_id} 的 "
                        f"{region.analysis_unit_id} 失敗；後續執行組未啟動，"
                        "已完成 family 保持不可覆寫。"
                    ) from error
        all_results.extend(
            results_by_unit[region.analysis_unit_id]
            for region in execution_group.regions
        )

    return FixedDepthBatchExecutionResult(
        batch_label=config.batch_label,
        result_namespace=config.result_namespace,
        figure_bundle_namespace=config.figure_bundle_namespace,
        visible_cpu_cores=visible_cpu_cores,
        execution_backend=execution_backend,
        regions=tuple(all_results),
        total_elapsed_seconds=time.perf_counter() - started_at,
    )
