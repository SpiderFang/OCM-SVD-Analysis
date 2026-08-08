"""以完整 flow domain 建立六層聯合流速—自由水面高度的直接 SVD。

本模組實作的科學狀態向量為每個 UTC 時次的表層 ``u/v``、固定水下 10、20、30、
40、50 m 的 ``u/v``，以及一次且僅一次的 ``eta``。六組流速不是各自分解；所有特徵
會串接為同一個時間欄的狀態向量，並在同一個矩陣中共享同一組時間 PC。``eta`` 是二維自由水面高度，沒有垂向層，
故不得複製到各流速深度，否則會人為放大其統計權重。

完整 flow domain 的主持人矩陣 ``A=(feature,time)`` 可能超過 RAM。本流程先以
float32 的時間主序暫存原始欄位，再依主持人要求的 eta→all-u→all-v feature 順序，以
float64、Fortran-order memory-map 建立無缺值的加權距平矩陣 ``A``；若薄型 LAPACK SVD
的保守記憶體估計超出設定預算，便改用 PROPACK 對同一個 ``A`` 進行設定模態數的
直接求解。後者只讀取 ``A @ v`` 與 ``A.T @ u`` 的時間分塊乘積，絕不建立 ``A.T @ A``
或 ``A @ A.T``。原始時間主序檔只是 I/O 中間檔，不是正式 SVD 的矩陣方向。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import resource
import shutil
import sys
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
from threadpoolctl import threadpool_limits

from .vertical_interpolation import (
    _horizontal_barycentric_interpolate,
    interpolate_velocity_to_target_z,
)
from .performance import PerformanceRecorder
from .surface_multivariate_svd import (
    ACADEMIC_REPORT_READY_V9,
    _academic_report_layout,
    _canonical_json_hash,
    _clip_land_polygons_to_extent,
    _iso_utc_from_ns,
    _load_geojson_land_polygons,
    _read_json_object,
    _require,
    _resolve_report_font,
    _resolve_workspace_data_path,
    _sha256_file_content,
    _write_json,
)


WATER_COLUMN_ANALYSIS_KIND = "water_column_multivariate_svd"
"""六層聯合 SVD 的設定識別，與表層產品命名空間分離。"""

WATER_COLUMN_CONFIG_SCHEMA = "1.0.0"
"""本模組接受的 JSON 設定 schema；變更資料矩陣語意時必須提升版本。"""

WATER_COLUMN_MATRIX_ORIENTATION = "feature_by_time"
"""正式水柱 SVD 的固定矩陣方向：列是主持人要求的 feature，欄是 UTC 時間。"""

WATER_COLUMN_MATRIX_ORDER = "F"
"""加權 ``A=(feature,time)`` memory-map 的儲存順序；以時間欄分塊時可保持連續 I/O。"""

WATER_COLUMN_FIGURE_SCHEMA = "2.2.0"
"""水柱圖面資產 schema；2.2 對齊表層圖的簡潔右下角向量參考尺。"""

WATER_COLUMN_FIGURE_STYLE = "academic_report_ready_water_column_independent_v2"
"""水柱獨立圖面版本；v2 修正標題、色條與表層式向量參考尺，不改變 SVD 數值結果或資料矩陣。"""

VELOCITY_LEVEL_DEPTHS_M = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0)
"""輸出速度層的順序；0 m 表示已發布表層流，非固定 datum 的 ``z=0`` 內插。"""

VELOCITY_LEVEL_IDS = (
    "surface",
    "z_minus_010m",
    "z_minus_020m",
    "z_minus_030m",
    "z_minus_040m",
    "z_minus_050m",
)
"""各速度層不可變的機器可讀 ID；順序必須與 ``VELOCITY_LEVEL_DEPTHS_M`` 一致。"""

VELOCITY_LEVEL_LABELS_ZH = (
    "表層流",
    "水下 10 m 流",
    "水下 20 m 流",
    "水下 30 m 流",
    "水下 40 m 流",
    "水下 50 m 流",
)
"""圖面與 metadata 使用的中文層位名稱，不將表層誤標為固定 ``z=0 m``。"""

NANOSECONDS_PER_HOUR = 3_600_000_000_000
"""UTC epoch 奈秒轉小時的精確整數常數，避免受本機時區影響。"""

SOLVER_RESUME_CHECKPOINT_SCHEMA = "2.0.0"
"""直接 SVD 可續跑 checkpoint 的資料格式版本。

checkpoint 僅存在於未發布的暫存／recovery 目錄，保存已完成的主持人方向加權異常矩陣所需欄位對應與
正規化常數。它不是科學成果，也不會進入最終 immutable 成果目錄；用途是在 PROPACK 或後續
圖面步驟失敗時，避免重新讀取兩年 native 3D 資料並重建數十 GiB 矩陣。
"""

SOLVER_RESUME_CHECKPOINT_FILENAME = "solver_resume_checkpoint.npz"
"""未發布暫存目錄內的直接 SVD 可續跑 checkpoint 檔名。"""

SOLVER_FAILURE_DIAGNOSTIC_FILENAME = "solver_failure_diagnostic.json"
"""未發布 recovery 目錄內保留 traceback 與矩陣資訊的失敗診斷檔名。"""

STREAMING_DIRECT_SVD_RESIDUAL_NUMERICAL_FLOOR = 1.0e-8
"""大型 memory-map 直接 SVD 的相對殘差數值驗證下限。

PROPACK 仍直接對主持人方向加權矩陣 ``A`` 求奇異三元組；但驗證階段會以另一組分塊 ``A @ v`` 與
``A.T @ u`` 重算殘差。對兩年、十萬以上特徵的矩陣，這兩次 streaming BLAS 累加的浮點捨入
誤差可高於人為設定的 ``1e-9``，即使奇異向量已收斂且正交性良好。此下限只界定「重新計算
殘差」的可信數值地板，絕不改變資料矩陣、SVD 演算法、模態數或物理權重；實際使用值與原始
設定值均會寫入 metadata，讓成果使用者能明確判讀。"""

STREAMING_DIRECT_SVD_ORTHOGONALITY_TOLERANCE = 1.0e-10
"""streaming 直接 SVD 發布前的最大正交性誤差上限。

殘差因分塊重算而可採用明載的數值地板時，仍須同時要求左右奇異向量的 Gram 矩陣接近單位
矩陣。這個門檻防止只因殘差落在數值地板內便發布線性相依或退化的模態。"""


@dataclass(frozen=True)
class WaterColumnConfig:
    """經驗證的六層聯合 SVD 科學與運算設定。

    ``domain_bbox`` 必須逐值對應上游 ``ocm_flow_domains.json`` 的完整 flow domain，
    不容許以舊有小型 SVD analysis unit 覆寫。``vertical_weights_m`` 是 0–50 m 六個
    採樣層的梯形積分權重，僅影響流速空間內積，不會把 eta 誤當作三維欄位。
    ``native_block_read_strategy`` 是明示的效能契約：它只決定如何從來源檔 materialize
    已選 native node，不得改變 node 集合、固定深度內插、缺值遮罩或最終 SVD 矩陣。正式
    SERVER run 必須把所選策略寫在設定與 metadata，不能依當下資源狀態暗中改變資料路徑。
    """

    raw: dict[str, Any]
    config_path: Path
    analysis_label: str
    domain_id: str
    domain_name_zh: str
    domain_bbox: tuple[float, float, float, float]
    domain_center: tuple[float, float]
    source_flow_domains_config: str
    source_flow_domains_config_sha256: str
    source_flow_domains_path: Path
    years: tuple[int, ...]
    months: tuple[int, ...]
    required_status: str
    required_cache_kinds: frozenset[str]
    required_cache_schema_major: int
    expected_timestep_hours: float
    maximum_source_gap_hours: float | None
    time_axis_policy: str
    fixed_depths_m: tuple[float, ...]
    vertical_weights_m: tuple[float, ...]
    minimum_feature_valid_fraction: float
    minimum_retained_time_fraction: float
    minimum_cells_per_velocity_level: int
    minimum_eta_cells: int
    requested_mode_count: int
    minimum_reported_mode_count: int
    dense_memory_limit_bytes: int
    operator_time_block_rows: int
    propack_maxiter: int
    propack_max_attempts: int
    residual_tolerance: float
    random_seed: int
    native_io_workers: int
    native_block_read_strategy: str
    linear_algebra_threads: int
    native_time_block_size: int
    figure_mode_count: int
    figure_formats: tuple[str, ...]
    figure_dpi: int
    max_quiver_arrows_per_axis: int
    figure_land_overlay_path: Path | None
    figure_land_overlay_logical_path: str | None
    figure_land_overlay_sha256: str | None


@dataclass(frozen=True)
class GridContext:
    """完整 flow domain 的規則格網與 native 水平內插對應。

    ``local_vertices`` 將 surface grid 已發布的 source node index 改寫為只讀取所需 native
    node 子集後的局部索引。``selected_node_run_count`` 量化節點索引在 native 軸上的破碎程度，
    作為選擇 I/O 設定的可稽核依據；實際讀取方法由 ``WaterColumnConfig`` 明確指定，而不是
    執行期間自行切換，確保效能調整與科學資料路徑彼此可分辨。
    """

    lon: np.ndarray
    lat: np.ndarray
    cell_area_m2: np.ndarray
    bathymetry_m: np.ndarray
    static_mask: np.ndarray
    geometry_mask: np.ndarray
    supported_mask: np.ndarray
    selected_nodes: np.ndarray
    source_node_count: int
    selected_node_run_count: int
    local_vertices: np.ndarray
    source_weights: np.ndarray


@dataclass(frozen=True)
class WaterColumnFigureGrid:
    """只供水柱圖面重繪使用的最小規則格網座標。

    完整 SVD 在 ``GridContext`` 中還需保存 native 節點內插、bathymetry 與來源權重；但既有
    科學 run 已經持久化 ``regression_*``、feature mask 與規則 ``lon/lat``，重繪時不能也
    不需要回讀 ``ocm_native``／``ocm_surface``。此類別刻意只保留繪圖軸所需座標，讓
    ``_make_water_column_figures`` 能同時服務完整求解與只讀成果重繪，並以型別契約阻止
    replot 流程誤觸任何資料矩陣建立或 SVD 計算。
    """

    lon: np.ndarray
    lat: np.ndarray


@dataclass(frozen=True)
class MonthDescriptor:
    """單月 paired cache 的 UTC 位置與來源 provenance。

    ``canonical_rows`` 將原月內 time index 映射至全 run 的嚴格遞增 UTC 軸；被去重的舊
    樣本為 -1。資料讀取時仍依原月內連續 slice 進行，只有寫入暫存矩陣時才使用此映射，
    因而不會因跨月排序而重排 native/surface 數值。
    """

    year: int
    month: int
    month_id: str
    source_metadata: dict[str, Any]
    native_metadata: dict[str, Any]
    cache_kind: str
    source_start: int
    source_stop: int
    canonical_rows: np.ndarray


@dataclass(frozen=True)
class Timeline:
    """經時間排序／去重後的聯合分析時間軸與來源月份摘要。"""

    time_utc_ns: np.ndarray
    descriptors: tuple[MonthDescriptor, ...]
    source_time_count: int
    reordered_time_step_count: int
    dropped_duplicate_time_step_count: int
    median_timestep_hours: float
    maximum_gap_hours: float
    gap_break_count: int


@dataclass(frozen=True)
class CandidateFeatureLayout:
    """以靜態 bathymetry 與支撐節點預先配置的暫存矩陣欄位。

    原始暫存矩陣先包含所有物理上可能的 feature；讀完全部時間後才依真實 ``zcor/u/v``
    有效率篩選，避免事前假定某一 sigma 層永遠可代表固定深度。每個速度層都保存配對的
    u/v 欄範圍，確保後續遮罩不會只留下單一流速分量。
    """

    level_rows: tuple[np.ndarray, ...]
    level_cols: tuple[np.ndarray, ...]
    level_u_slices: tuple[slice, ...]
    level_v_slices: tuple[slice, ...]
    eta_rows: np.ndarray
    eta_cols: np.ndarray
    eta_slice: slice
    sqrt_metric_weight: np.ndarray
    feature_group: np.ndarray

    @property
    def feature_count(self) -> int:
        """回傳 raw 暫存矩陣的總特徵數。"""

        return int(self.sqrt_metric_weight.size)


@dataclass(frozen=True)
class SelectedFeatureLayout:
    """通過實際可用率篩選後，真正進入單次 SVD 的欄位對應。

    各深度可以有不同的有效水平範圍；這不是分層 SVD，而是在同一個向量中只保留該深度
    真正存在的 u/v feature。正式布局遵循主持人方向：eta 一次置於最前，接著所有深度
    的 u，再接著所有深度的 v。所有層仍共用同一組時間樣本、奇異值、空間模態排序與 PC。
    ``feature_valid_fraction`` 雖隨本布局保存，但其軸仍是完整 candidate raw feature 軸，而非
    只保留欄位：它用來重開成完整 QC 有效率地圖，並在建立加權矩陣前辨識 raw memory-map 的
    欄數。真正進入 SVD 的子集由 ``selected_raw_columns`` 指定。
    """

    level_rows: tuple[np.ndarray, ...]
    level_cols: tuple[np.ndarray, ...]
    level_u_slices: tuple[slice, ...]
    level_v_slices: tuple[slice, ...]
    eta_rows: np.ndarray
    eta_cols: np.ndarray
    eta_slice: slice
    selected_raw_columns: np.ndarray
    sqrt_metric_weight: np.ndarray
    feature_group: np.ndarray
    feature_selection_policy: str
    feature_valid_fraction: np.ndarray

    @property
    def feature_count(self) -> int:
        """回傳最終 SVD 矩陣的欄數。"""

        return int(self.selected_raw_columns.size)


@dataclass(frozen=True)
class DirectSvdResult:
    """直接 SVD 或 PROPACK 設定模態數奇異三元組的統一結果。

    正式矩陣是主持人要求的 ``A=(feature,time)``，因此 ``u`` 維度為
    ``(feature, mode)``，``vh`` 為 ``(mode, time)``；兩者都已依奇異值由大到小排序，
    但尚未固定 SVD 正負號。``solver_metadata`` 完整揭露是否使用 dense LAPACK 或
    memory-map PROPACK，以及收斂／殘差證據。
    """

    u: np.ndarray
    singular_values: np.ndarray
    vh: np.ndarray
    total_sum_squares: float
    retained_reconstruction_error: float
    orthogonality_max_abs_error: float
    left_residuals: np.ndarray
    right_residuals: np.ndarray
    solver_metadata: dict[str, Any]


@dataclass(frozen=True)
class SolverResumeState:
    """從未發布 recovery checkpoint 還原直接 SVD 所需的最小科學狀態。

    兩年原生 ``hvel/zcor`` 已完成讀取、固定深度內插與缺值篩選後，PROPACK 只需要已標準化的
    ``weighted_anomaly_float64.dat`` 及本類別保存的欄位對應、平均值、尺度與 UTC 時軸。保留
    這些資料可以在求解器容差、迭代預算或繪圖流程失敗時，重新使用完全相同的矩陣；不得用它
    偷換資料範圍、變更 mask，或把 eta 複製成多個深度欄位。
    """

    selected: SelectedFeatureLayout
    retained_time: np.ndarray
    feature_mean: np.ndarray
    velocity_rms: float
    eta_rms: float
    feature_scale: np.ndarray
    total_sum_squares: float


def _array_sha256(array: np.ndarray) -> str:
    """回傳一維／多維 ndarray 的資料與 shape 可重現 SHA-256 摘要。

    續跑前必須確認 checkpoint 對應的 canonical UTC 軸沒有被上游 cache 改寫。只雜湊 raw
    bytes 會讓不同 shape 的同位元組數資料碰撞，因此摘要同時包含 dtype 與 shape；本函式只
    用於小型座標／時間陣列，不會複製數十 GiB 的加權 SVD 矩陣到記憶體。
    """

    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("utf-8"))
    digest.update(np.asarray(contiguous.shape, dtype=np.int64).tobytes())
    digest.update(memoryview(contiguous).cast("B"))
    return digest.hexdigest()


def _slice_bounds(slices: Sequence[slice], *, field: str) -> np.ndarray:
    """把 numpy 欄位 slice 轉為可安全寫入 NPZ 的 ``(start, stop)`` 整數陣列。

    ``SelectedFeatureLayout`` 使用 slice 表示各深度 u/v 與 eta 在單一狀態向量的位置；slice
    物件不可直接安全序列化為 ``allow_pickle=False`` 的 NPZ，故在 checkpoint 中改存明確邊界。
    未設定或倒置的邊界代表資料布局損毀，必須拒絕續跑而非猜測欄位。
    """

    bounds: list[tuple[int, int]] = []
    for index, item in enumerate(slices):
        _require(item.step in (None, 1), f"{field}[{index}] 只能使用連續正向欄位")
        _require(item.start is not None and item.stop is not None, f"{field}[{index}] 必須有明確 start/stop")
        start = int(item.start)
        stop = int(item.stop)
        _require(0 <= start <= stop, f"{field}[{index}] 的欄位邊界不合法")
        bounds.append((start, stop))
    return np.asarray(bounds, dtype=np.int64)


def _slices_from_bounds(bounds: np.ndarray, *, field: str, expected_count: int) -> tuple[slice, ...]:
    """驗證 checkpoint 欄位邊界並重建不可變的 slice tuple。"""

    normalized = np.asarray(bounds, dtype=np.int64)
    _require(normalized.shape == (expected_count, 2), f"{field} checkpoint shape 必須是 ({expected_count}, 2)")
    _require(np.all(normalized[:, 0] >= 0) and np.all(normalized[:, 0] <= normalized[:, 1]), f"{field} checkpoint 欄位邊界不合法")
    return tuple(slice(int(start), int(stop)) for start, stop in normalized)


def _write_solver_resume_checkpoint(
    checkpoint_path: Path,
    *,
    config: WaterColumnConfig,
    timeline: Timeline,
    candidate: CandidateFeatureLayout,
    selected: SelectedFeatureLayout,
    retained_time: np.ndarray,
    feature_mean: np.ndarray,
    velocity_rms: float,
    eta_rms: float,
    feature_scale: np.ndarray,
    total_sum_squares: float,
) -> None:
    """原子寫入可重用同一加權矩陣的直接 SVD checkpoint。

    寫入時機位於 raw feature matrix 刪除後、PROPACK 開始前：此時 checkpoint 與
    ``weighted_anomaly_float64.dat`` 已足以完全重現同一個輸入矩陣，且不再需要兩年 NFS 原生
    資料。checkpoint 明確綁定設定內容、canonical UTC 軸與候選 feature 數，避免錯誤拿別次
    run 或不同 mask 的暫存矩陣續跑。
    """

    _require(selected.feature_count == feature_mean.size == feature_scale.size, "checkpoint 的 selected feature、平均值與尺度長度必須一致")
    _require(
        selected.feature_valid_fraction.shape == (candidate.feature_count,),
        "checkpoint 的 candidate feature 有效率必須保留完整 raw 欄軸",
    )
    _require(retained_time.ndim == 1 and retained_time.size > 0, "checkpoint 必須保存至少一個保留 UTC 時次")
    _require(np.all(np.diff(retained_time) > 0), "checkpoint 保留 UTC 時軸必須嚴格遞增")
    _require(math.isfinite(velocity_rms) and velocity_rms > 0.0, "checkpoint velocity RMS 必須為有限正值")
    _require(math.isfinite(eta_rms) and eta_rms > 0.0, "checkpoint eta RMS 必須為有限正值")
    _require(math.isfinite(total_sum_squares) and total_sum_squares > 0.0, "checkpoint 總變異必須為有限正值")

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = checkpoint_path.with_name(f".{checkpoint_path.stem}.{uuid.uuid4().hex}.partial.npz")
    payload: dict[str, np.ndarray] = {
        "schema_version": np.asarray(SOLVER_RESUME_CHECKPOINT_SCHEMA),
        "weighted_matrix_orientation": np.asarray(WATER_COLUMN_MATRIX_ORIENTATION),
        "weighted_matrix_storage_order": np.asarray(WATER_COLUMN_MATRIX_ORDER),
        "analysis_label": np.asarray(config.analysis_label),
        "config_sha256": np.asarray(_canonical_json_hash(config.raw)),
        "canonical_time_sha256": np.asarray(_array_sha256(timeline.time_utc_ns)),
        "candidate_feature_count": np.asarray(candidate.feature_count, dtype=np.int64),
        "retained_time_utc_ns": np.asarray(retained_time, dtype=np.int64),
        "feature_mean": np.asarray(feature_mean, dtype=np.float64),
        "velocity_rms": np.asarray(velocity_rms, dtype=np.float64),
        "eta_rms": np.asarray(eta_rms, dtype=np.float64),
        "feature_scale": np.asarray(feature_scale, dtype=np.float64),
        "total_sum_squares": np.asarray(total_sum_squares, dtype=np.float64),
        "selected_level_u_bounds": _slice_bounds(selected.level_u_slices, field="selected.level_u_slices"),
        "selected_level_v_bounds": _slice_bounds(selected.level_v_slices, field="selected.level_v_slices"),
        "selected_eta_bounds": _slice_bounds((selected.eta_slice,), field="selected.eta_slice"),
        "selected_raw_columns": np.asarray(selected.selected_raw_columns, dtype=np.int64),
        "selected_sqrt_metric_weight": np.asarray(selected.sqrt_metric_weight, dtype=np.float64),
        "selected_feature_group": np.asarray(selected.feature_group, dtype=np.int8),
        "selected_feature_selection_policy": np.asarray(selected.feature_selection_policy),
        "selected_feature_valid_fraction": np.asarray(selected.feature_valid_fraction, dtype=np.float64),
    }
    for level_index, (rows, cols) in enumerate(zip(selected.level_rows, selected.level_cols, strict=True)):
        payload[f"selected_level_rows_{level_index}"] = np.asarray(rows, dtype=np.int64)
        payload[f"selected_level_cols_{level_index}"] = np.asarray(cols, dtype=np.int64)
    payload["selected_eta_rows"] = np.asarray(selected.eta_rows, dtype=np.int64)
    payload["selected_eta_cols"] = np.asarray(selected.eta_cols, dtype=np.int64)
    try:
        np.savez(temporary_path, **payload)
        os.replace(temporary_path, checkpoint_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _load_solver_resume_checkpoint(
    checkpoint_path: Path,
    weighted_matrix_path: Path,
    *,
    config: WaterColumnConfig,
    timeline: Timeline,
    candidate: CandidateFeatureLayout,
) -> SolverResumeState:
    """驗證並載入 recovery checkpoint，不接受資料或設定不相符的續跑。

    恢復時仍重新讀取小型 grid／月 metadata 與 canonical UTC 軸，藉此確認上游 cache 未被替換；
    不會重新 materialize 大型 native ``hvel/zcor``。只有 checkpoint、加權矩陣尺寸、設定 hash
    與 UTC 摘要全部一致時才回傳狀態，否則要求重新建立資料矩陣以維持科學可追溯性。
    """

    _require(checkpoint_path.is_file(), f"找不到可續跑 checkpoint: {checkpoint_path}")
    _require(weighted_matrix_path.is_file(), f"找不到可續跑加權矩陣: {weighted_matrix_path}")
    required_keys = {
        "schema_version",
        "weighted_matrix_orientation",
        "weighted_matrix_storage_order",
        "analysis_label",
        "config_sha256",
        "canonical_time_sha256",
        "candidate_feature_count",
        "retained_time_utc_ns",
        "feature_mean",
        "velocity_rms",
        "eta_rms",
        "feature_scale",
        "total_sum_squares",
        "selected_level_u_bounds",
        "selected_level_v_bounds",
        "selected_eta_bounds",
        "selected_raw_columns",
        "selected_sqrt_metric_weight",
        "selected_feature_group",
        "selected_feature_selection_policy",
        "selected_feature_valid_fraction",
        "selected_eta_rows",
        "selected_eta_cols",
    }
    required_keys.update({f"selected_level_rows_{index}" for index in range(len(VELOCITY_LEVEL_IDS))})
    required_keys.update({f"selected_level_cols_{index}" for index in range(len(VELOCITY_LEVEL_IDS))})
    with np.load(checkpoint_path, allow_pickle=False) as archive:
        missing = sorted(required_keys.difference(archive.files))
        _require(not missing, f"checkpoint 缺少必要欄位: {missing}")
        _require(str(archive["schema_version"].item()) == SOLVER_RESUME_CHECKPOINT_SCHEMA, "checkpoint schema_version 不支援")
        _require(str(archive["weighted_matrix_orientation"].item()) == WATER_COLUMN_MATRIX_ORIENTATION, "checkpoint 加權矩陣方向不是主持人要求的 feature×time")
        _require(str(archive["weighted_matrix_storage_order"].item()) == WATER_COLUMN_MATRIX_ORDER, "checkpoint 加權矩陣儲存順序不符")
        _require(str(archive["analysis_label"].item()) == config.analysis_label, "checkpoint analysis_label 與本次設定不符")
        _require(str(archive["config_sha256"].item()) == _canonical_json_hash(config.raw), "checkpoint 設定內容與本次設定不符")
        _require(str(archive["canonical_time_sha256"].item()) == _array_sha256(timeline.time_utc_ns), "checkpoint canonical UTC 軸與目前 paired cache 不符")
        _require(int(archive["candidate_feature_count"].item()) == candidate.feature_count, "checkpoint candidate feature 數與目前格網不符")

        level_rows = tuple(np.asarray(archive[f"selected_level_rows_{index}"], dtype=np.int64) for index in range(len(VELOCITY_LEVEL_IDS)))
        level_cols = tuple(np.asarray(archive[f"selected_level_cols_{index}"], dtype=np.int64) for index in range(len(VELOCITY_LEVEL_IDS)))
        selected = SelectedFeatureLayout(
            level_rows=level_rows,
            level_cols=level_cols,
            level_u_slices=_slices_from_bounds(archive["selected_level_u_bounds"], field="selected_level_u_bounds", expected_count=len(VELOCITY_LEVEL_IDS)),
            level_v_slices=_slices_from_bounds(archive["selected_level_v_bounds"], field="selected_level_v_bounds", expected_count=len(VELOCITY_LEVEL_IDS)),
            eta_rows=np.asarray(archive["selected_eta_rows"], dtype=np.int64),
            eta_cols=np.asarray(archive["selected_eta_cols"], dtype=np.int64),
            eta_slice=_slices_from_bounds(archive["selected_eta_bounds"], field="selected_eta_bounds", expected_count=1)[0],
            selected_raw_columns=np.asarray(archive["selected_raw_columns"], dtype=np.int64),
            sqrt_metric_weight=np.asarray(archive["selected_sqrt_metric_weight"], dtype=np.float64),
            feature_group=np.asarray(archive["selected_feature_group"], dtype=np.int8),
            feature_selection_policy=str(archive["selected_feature_selection_policy"].item()),
            feature_valid_fraction=np.asarray(archive["selected_feature_valid_fraction"], dtype=np.float64),
        )
        retained_time = np.asarray(archive["retained_time_utc_ns"], dtype=np.int64)
        feature_mean = np.asarray(archive["feature_mean"], dtype=np.float64)
        feature_scale = np.asarray(archive["feature_scale"], dtype=np.float64)
        velocity_rms = float(archive["velocity_rms"].item())
        eta_rms = float(archive["eta_rms"].item())
        total_sum_squares = float(archive["total_sum_squares"].item())

    feature_count = selected.feature_count
    _require(feature_count > 0, "checkpoint 的 selected feature 不得為空")
    _require(selected.sqrt_metric_weight.shape == (feature_count,), "checkpoint selected metric weight 長度不符")
    _require(selected.feature_group.shape == (feature_count,), "checkpoint selected feature group 長度不符")
    # 此欄位是完整 candidate raw 軸的有效率，而非 selected feature 軸。它在 resume 後仍要
    # 輸出每個候選格點的 QC 地圖，且一開始壓縮 float32 raw matrix 時也需依它辨識 source
    # feature 欄數；若誤以 selected feature 數驗證，任何因 NaN 排除欄位的正確 checkpoint
    # 都會被錯誤拒絕，迫使不必要地重讀兩年 native 3D 資料。
    _require(
        selected.feature_valid_fraction.shape == (candidate.feature_count,),
        "checkpoint candidate valid fraction 長度不符",
    )
    _require(
        np.all(np.isfinite(selected.feature_valid_fraction))
        and np.all((selected.feature_valid_fraction >= 0.0) & (selected.feature_valid_fraction <= 1.0)),
        "checkpoint candidate valid fraction 必須介於 0 與 1",
    )
    _require(feature_mean.shape == (feature_count,), "checkpoint feature_mean 長度不符")
    _require(feature_scale.shape == (feature_count,), "checkpoint feature_scale 長度不符")
    _require(
        retained_time.ndim == 1 and retained_time.size >= config.requested_mode_count,
        "checkpoint 保留 UTC 時次少於設定要求的模態數",
    )
    _require(np.all(np.diff(retained_time) > 0), "checkpoint 保留 UTC 時軸必須嚴格遞增")
    _require(np.all(np.isin(retained_time, timeline.time_utc_ns)), "checkpoint 保留 UTC 時次不是目前 canonical 軸的子集")
    _require(np.all(np.isfinite(feature_mean)) and np.all(np.isfinite(feature_scale)) and np.all(feature_scale > 0.0), "checkpoint 特徵平均值或尺度不合法")
    _require(math.isfinite(velocity_rms) and velocity_rms > 0.0, "checkpoint velocity RMS 不合法")
    _require(math.isfinite(eta_rms) and eta_rms > 0.0, "checkpoint eta RMS 不合法")
    _require(math.isfinite(total_sum_squares) and total_sum_squares > 0.0, "checkpoint 總變異不合法")
    expected_matrix_bytes = retained_time.size * feature_count * np.dtype(np.float64).itemsize
    _require(weighted_matrix_path.stat().st_size == expected_matrix_bytes, "checkpoint 加權矩陣尺寸與 UTC／feature 布局不符")
    return SolverResumeState(
        selected=selected,
        retained_time=retained_time,
        feature_mean=feature_mean,
        velocity_rms=velocity_rms,
        eta_rms=eta_rms,
        feature_scale=feature_scale,
        total_sum_squares=total_sum_squares,
    )


def _write_solver_failure_diagnostic(
    diagnostic_path: Path,
    *,
    error: BaseException,
    checkpoint_path: Path,
    weighted_matrix_path: Path,
    resumed_from_recovery: bool,
) -> None:
    """在可續跑 checkpoint 旁寫入失敗診斷，讓無 tmux 歷史時仍可追查 traceback。

    這個檔案刻意放在未發布 recovery 目錄，而非最終成果，內容只描述 exception、checkpoint
    位置及暫存矩陣大小，不含密碼、環境變數或原始資料值。下一次續跑可覆寫其內容，代表最新
    一次未完成的嘗試；正式發布前會把它與大型 checkpoint 一併移除。
    """

    payload = {
        "schema_name": "ocm_water_column_svd_recovery_diagnostic",
        "schema_version": "1.0.0",
        "status": "failed_recoverable",
        "resumed_from_recovery": resumed_from_recovery,
        "exception_type": type(error).__name__,
        "exception_message": str(error),
        "traceback": traceback.format_exc(),
        "checkpoint_filename": checkpoint_path.name,
        "checkpoint_exists": checkpoint_path.is_file(),
        "weighted_matrix_filename": weighted_matrix_path.name,
        "weighted_matrix_bytes": weighted_matrix_path.stat().st_size if weighted_matrix_path.is_file() else None,
    }
    _write_json(diagnostic_path, payload)


def _as_positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    """驗證設定中的模式數、區塊大小與重試次數為安全整數。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必須是{'非負' if allow_zero else '正'}整數")
    if value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{field} 必須是{'非負' if allow_zero else '正'}整數")
    return int(value)


def _as_finite_float(value: object, field: str) -> float:
    """驗證 JSON 數值有限，避免 NaN/Infinity 汙染物理權重或容差。"""

    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field} 必須是有限數值")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} 必須是有限數值")
    return result


def _as_fraction(value: object, field: str) -> float:
    """驗證有效率／殘差門檻位於閉區間 0–1。"""

    result = _as_finite_float(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} 必須介於 0 與 1")
    return result


def _as_byte_limit_gib(value: object, field: str) -> int:
    """把 GiB 設定轉成整數位元組，避免在資源選擇時混淆 GB/GiB。"""

    gib = _as_finite_float(value, field)
    if gib <= 0.0:
        raise ValueError(f"{field} 必須為正數")
    return int(round(gib * 1024**3))


def _resolve_domain_config(
    raw: dict[str, Any],
    config_path: Path,
) -> tuple[str, str, tuple[float, float, float, float], tuple[float, float], str, str, Path]:
    """驗證本 run 的完整 domain 定義逐值來自上游 flow-domain 設定。

    這個驗證取代舊有 ``svd_analysis_units`` 契約：新研究單位就是完整 flow domain，
    因此任何 bbox、中心點或名稱差異都視為設定錯誤，而不是允許本專案局部自行修改。
    """

    domain = raw.get("domain")
    if not isinstance(domain, dict):
        raise ValueError("domain 必須是物件")
    domain_id = domain.get("flow_domain_id")
    name_zh = domain.get("name_zh")
    source_logical_path = domain.get("source_flow_domains_config")
    expected_hash = domain.get("source_flow_domains_config_sha256")
    if not all(isinstance(value, str) and value.strip() for value in (domain_id, name_zh, source_logical_path, expected_hash)):
        raise ValueError("domain.flow_domain_id、name_zh、source_flow_domains_config 與 SHA-256 必須是非空白文字")
    if len(expected_hash) != 64 or any(character not in "0123456789abcdef" for character in expected_hash):
        raise ValueError("domain.source_flow_domains_config_sha256 必須是 64 字元小寫 SHA-256")
    source_path = _resolve_workspace_data_path(config_path, source_logical_path)
    actual_hash = _sha256_file_content(source_path)
    _require(actual_hash == expected_hash, "上游 ocm_flow_domains.json SHA-256 與設定不一致；必須建立新分析版本")
    source_payload = _read_json_object(source_path)
    domains = source_payload.get("flow_domains")
    if not isinstance(domains, list):
        raise ValueError("上游 ocm_flow_domains.json.flow_domains 必須是清單")
    upstream = next(
        (
            item
            for item in domains
            if isinstance(item, dict) and item.get("flow_domain_id") == domain_id
        ),
        None,
    )
    if upstream is None:
        raise ValueError(f"上游 ocm_flow_domains.json 找不到 flow_domain_id={domain_id}")
    upstream_bbox = upstream.get("bbox")
    upstream_center = upstream.get("center")
    if not isinstance(upstream_bbox, list) or len(upstream_bbox) != 4:
        raise ValueError("上游 flow domain bbox 必須是四元素清單")
    if not isinstance(upstream_center, list) or len(upstream_center) != 2:
        raise ValueError("上游 flow domain center 必須是二元素清單")
    bbox = tuple(_as_finite_float(value, "upstream flow domain bbox") for value in upstream_bbox)
    center = tuple(_as_finite_float(value, "upstream flow domain center") for value in upstream_center)
    if not (bbox[0] < bbox[1] and bbox[2] < bbox[3]):
        raise ValueError("上游 flow domain bbox 必須滿足 min < max")
    config_bbox = domain.get("bbox_lon_lat")
    config_center = domain.get("center_lonlat")
    if not isinstance(config_bbox, list) or len(config_bbox) != 4:
        raise ValueError("domain.bbox_lon_lat 必須是 [lon_min, lon_max, lat_min, lat_max]")
    if not isinstance(config_center, list) or len(config_center) != 2:
        raise ValueError("domain.center_lonlat 必須是 [lon, lat]")
    parsed_bbox = tuple(_as_finite_float(value, "domain.bbox_lon_lat") for value in config_bbox)
    parsed_center = tuple(_as_finite_float(value, "domain.center_lonlat") for value in config_center)
    _require(parsed_bbox == bbox, "domain.bbox_lon_lat 必須逐值對應上游完整 flow domain bbox")
    _require(parsed_center == center, "domain.center_lonlat 必須逐值對應上游完整 flow domain center")
    _require(upstream.get("name_zh") == name_zh, "domain.name_zh 必須對應上游 flow domain 名稱")
    return (
        str(domain_id),
        str(name_zh),
        bbox,
        center,
        str(source_logical_path),
        str(expected_hash),
        source_path,
    )


def load_water_column_config(config_path: Path) -> WaterColumnConfig:
    """讀取六層聯合 SVD 設定並驗證資料矩陣與資源策略。

    設定把「表層」明確定義為已發布的表層流，而固定物理深度只適用於 10–50 m。這避免
    將主持人說的表層誤實作成潮位變動下可能在水面外的 datum ``z=0`` 速度。
    """

    config_path = config_path.resolve()
    raw = _read_json_object(config_path)
    _require(raw.get("schema_version") == WATER_COLUMN_CONFIG_SCHEMA, f"設定檔必須使用 schema {WATER_COLUMN_CONFIG_SCHEMA}")
    _require(raw.get("analysis_kind") == WATER_COLUMN_ANALYSIS_KIND, f"analysis_kind 必須是 {WATER_COLUMN_ANALYSIS_KIND}")
    analysis_label = raw.get("analysis_label")
    if not isinstance(analysis_label, str) or not analysis_label.strip():
        raise ValueError("analysis_label 必須是非空白文字")
    _require(analysis_label.rstrip().endswith(tuple(f"_v{number}" for number in range(1, 1000))), "analysis_label 必須以 _vN 結尾，保護不可覆寫科學版本")
    (
        domain_id,
        domain_name_zh,
        domain_bbox,
        domain_center,
        source_flow_domains_config,
        source_flow_domains_hash,
        source_flow_domains_path,
    ) = _resolve_domain_config(raw, config_path)

    input_config = raw.get("input")
    if not isinstance(input_config, dict):
        raise ValueError("input 必須是物件")
    years_raw = input_config.get("years")
    months_raw = input_config.get("months")
    if not isinstance(years_raw, list) or not years_raw:
        raise ValueError("input.years 必須是非空白年份清單")
    if not isinstance(months_raw, list) or not months_raw:
        raise ValueError("input.months 必須是非空白月份清單")
    years = tuple(_as_positive_int(value, "input.years") for value in years_raw)
    months = tuple(_as_positive_int(value, "input.months") for value in months_raw)
    _require(years == tuple(sorted(set(years))), "input.years 必須由小到大且不可重複")
    _require(months == tuple(sorted(set(months))) and all(1 <= month <= 12 for month in months), "input.months 必須由小到大、不可重複且介於 1–12")
    required_status = input_config.get("required_status")
    kinds_raw = input_config.get("required_cache_kinds")
    if not isinstance(required_status, str) or not required_status:
        raise ValueError("input.required_status 必須是非空白文字")
    if not isinstance(kinds_raw, list) or not kinds_raw or not all(isinstance(item, str) and item for item in kinds_raw):
        raise ValueError("input.required_cache_kinds 必須是非空白文字清單")
    schema_major = _as_positive_int(input_config.get("required_cache_schema_major"), "input.required_cache_schema_major")
    _require(schema_major == 3, "目前只接受 OCM cache schema 3.x")
    timestep_hours = _as_finite_float(input_config.get("expected_timestep_hours"), "input.expected_timestep_hours")
    _require(timestep_hours > 0.0, "input.expected_timestep_hours 必須為正數")
    maximum_gap_raw = input_config.get("maximum_source_gap_hours")
    maximum_gap = None if maximum_gap_raw is None else _as_finite_float(maximum_gap_raw, "input.maximum_source_gap_hours")
    if maximum_gap is not None:
        _require(maximum_gap > 0.0, "input.maximum_source_gap_hours 若非 null 必須為正數")
    time_axis_policy = input_config.get("time_axis_canonicalization_policy")
    _require(time_axis_policy in {"reject", "sort_and_deduplicate_prefer_last"}, "input.time_axis_canonicalization_policy 必須是 reject 或 sort_and_deduplicate_prefer_last")

    vertical = raw.get("vertical_sampling")
    if not isinstance(vertical, dict):
        raise ValueError("vertical_sampling 必須是物件")
    _require(vertical.get("surface_velocity_source") == "published_ocm_surface_u_v", "vertical_sampling.surface_velocity_source 必須是 published_ocm_surface_u_v")
    _require(vertical.get("eta_source") == "published_ocm_surface_eta_m_once", "vertical_sampling.eta_source 必須明確為一次且僅一次的 published_ocm_surface_eta_m_once")
    _require(vertical.get("vertical_interpolation") == "linear_between_bracketing_finite_zcor_no_extrapolation", "vertical_sampling.vertical_interpolation 必須禁止垂向外插")
    fixed_depths_raw = vertical.get("fixed_depths_m_below_vertical_datum")
    if not isinstance(fixed_depths_raw, list):
        raise ValueError("vertical_sampling.fixed_depths_m_below_vertical_datum 必須是清單")
    fixed_depths = tuple(_as_finite_float(value, "vertical_sampling.fixed_depths_m_below_vertical_datum") for value in fixed_depths_raw)
    _require(fixed_depths == VELOCITY_LEVEL_DEPTHS_M[1:], "固定深度必須是 10、20、30、40、50 m；表層由 ocm_surface 提供")
    vertical_weights_raw = vertical.get("vertical_quadrature_weights_m")
    if not isinstance(vertical_weights_raw, list) or len(vertical_weights_raw) != len(VELOCITY_LEVEL_DEPTHS_M):
        raise ValueError("vertical_sampling.vertical_quadrature_weights_m 必須對應六個速度層")
    vertical_weights = tuple(_as_finite_float(value, "vertical_sampling.vertical_quadrature_weights_m") for value in vertical_weights_raw)
    _require(all(weight > 0.0 for weight in vertical_weights), "垂向積分權重必須為正")

    mask = raw.get("mask_and_missing_data")
    if not isinstance(mask, dict):
        raise ValueError("mask_and_missing_data 必須是物件")
    _require(mask.get("static_ocean_mask") == "mask_static.npy", "mask_and_missing_data.static_ocean_mask 必須是 mask_static.npy")
    _require(mask.get("missing_value_policy") == "never_fill_nan_with_zero_or_interpolate", "本直接 SVD 只能使用 never_fill_nan_with_zero_or_interpolate 缺值政策")
    minimum_feature_valid_fraction = _as_fraction(mask.get("minimum_feature_valid_fraction"), "mask_and_missing_data.minimum_feature_valid_fraction")
    minimum_retained_time_fraction = _as_fraction(mask.get("minimum_retained_time_fraction"), "mask_and_missing_data.minimum_retained_time_fraction")
    minimum_cells_per_velocity_level = _as_positive_int(mask.get("minimum_cells_per_velocity_level"), "mask_and_missing_data.minimum_cells_per_velocity_level")
    minimum_eta_cells = _as_positive_int(mask.get("minimum_eta_cells"), "mask_and_missing_data.minimum_eta_cells")

    svd = raw.get("svd")
    if not isinstance(svd, dict):
        raise ValueError("svd 必須是物件")
    _require(svd.get("variables") == ["u_velocity_mps", "v_velocity_mps", "eta_m"], "svd.variables 必須依序為 u_velocity_mps、v_velocity_mps、eta_m")
    _require(svd.get("solver_policy") == "direct_dense_lapack_then_direct_propack_streaming", "svd.solver_policy 必須明確指定 direct dense/PROPACK 策略")
    _require(svd.get("spatial_weight") == "sqrt(cell_area_m2_times_vertical_quadrature_for_u_v__sqrt(cell_area_m2)_for_eta", "svd.spatial_weight 必須明載 u/v 的體積近似權重與 eta 的面積權重")
    _require(svd.get("normalization") == "u_v_share_volume_weighted_rms__eta_uses_area_weighted_rms", "svd.normalization 必須使用群組加權 RMS，不可暗中改為逐格 z-score")
    requested_mode_count = _as_positive_int(svd.get("requested_mode_count"), "svd.requested_mode_count")
    minimum_reported_mode_count = _as_positive_int(svd.get("minimum_reported_mode_count"), "svd.minimum_reported_mode_count")
    _require(
        minimum_reported_mode_count <= requested_mode_count,
        "svd.minimum_reported_mode_count 不得大於 svd.requested_mode_count",
    )

    solver = raw.get("solver")
    if not isinstance(solver, dict):
        raise ValueError("solver 必須是物件")
    _require(solver.get("dense_solver") == "numpy_linalg_svd_full_matrices_false", "solver.dense_solver 必須是 numpy_linalg_svd_full_matrices_false")
    _require(solver.get("streaming_solver") == "scipy_svds_propack_linear_operator", "solver.streaming_solver 必須是 scipy_svds_propack_linear_operator")
    dense_memory_limit = _as_byte_limit_gib(solver.get("dense_memory_limit_gib"), "solver.dense_memory_limit_gib")
    operator_time_block_rows = _as_positive_int(solver.get("operator_time_block_rows"), "solver.operator_time_block_rows")
    propack_maxiter = _as_positive_int(solver.get("propack_maxiter"), "solver.propack_maxiter")
    propack_max_attempts = _as_positive_int(solver.get("propack_max_attempts"), "solver.propack_max_attempts")
    residual_tolerance = _as_finite_float(solver.get("relative_residual_tolerance"), "solver.relative_residual_tolerance")
    _require(0.0 < residual_tolerance < 1.0, "solver.relative_residual_tolerance 必須介於 0 與 1")
    random_seed = _as_positive_int(solver.get("random_seed"), "solver.random_seed", allow_zero=True)

    parallel = raw.get("parallel_execution")
    if not isinstance(parallel, dict):
        raise ValueError("parallel_execution 必須是物件")
    native_io_workers = _as_positive_int(
        parallel.get("native_io_workers", 1),
        "parallel_execution.native_io_workers",
    )
    native_block_read_strategy = parallel.get("native_block_read_strategy", "selected_nodes_fancy_index")
    _require(
        native_block_read_strategy in {"selected_nodes_fancy_index", "contiguous_full_source_axis_then_select"},
        "parallel_execution.native_block_read_strategy 必須是 selected_nodes_fancy_index 或 contiguous_full_source_axis_then_select",
    )
    linear_algebra_threads = _as_positive_int(parallel.get("linear_algebra_threads"), "parallel_execution.linear_algebra_threads")
    native_time_block_size = _as_positive_int(parallel.get("native_time_block_size"), "parallel_execution.native_time_block_size")

    figures = raw.get("figures")
    if not isinstance(figures, dict):
        raise ValueError("figures 必須是物件")
    figure_mode_count = _as_positive_int(figures.get("mode_count"), "figures.mode_count")
    _require(
        figure_mode_count <= requested_mode_count,
        "figures.mode_count 不得大於 svd.requested_mode_count",
    )
    formats_raw = figures.get("output_formats")
    if not isinstance(formats_raw, list) or not formats_raw or not all(item in {"png", "svg"} for item in formats_raw):
        raise ValueError("figures.output_formats 必須是 png/svg 的非空白清單")
    _require(len(formats_raw) == len(set(formats_raw)), "figures.output_formats 不可重複")
    # 圖面底圖不是 SVD 的分析遮罩，但正式報告需要能追溯所使用的海岸線資料。為了維持
    # 舊合成測試設定與既有數值 run 的相容性，這三個欄位採可選契約；正式 flow-domain
    # 設定若提供其中任一欄，就必須同時提供完整路徑與 SHA-256，且內容雜湊必須吻合。
    coastline_logical_path = figures.get("coastline_geojson")
    coastline_sha256 = figures.get("coastline_geojson_sha256")
    if coastline_logical_path is None and coastline_sha256 is None:
        coastline_path = None
    else:
        if not isinstance(coastline_logical_path, str) or not coastline_logical_path.strip():
            raise ValueError("figures.coastline_geojson 必須是非空白路徑")
        if not isinstance(coastline_sha256, str) or len(coastline_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in coastline_sha256
        ):
            raise ValueError("figures.coastline_geojson_sha256 必須是 64 字元小寫 SHA-256")
        coastline_path = _resolve_workspace_data_path(config_path, coastline_logical_path)
        _require(
            _sha256_file_content(coastline_path) == coastline_sha256,
            "figures.coastline_geojson 內容與設定 SHA-256 不符；必須更新圖面版本與 provenance",
        )

    return WaterColumnConfig(
        raw=raw,
        config_path=config_path,
        analysis_label=analysis_label,
        domain_id=domain_id,
        domain_name_zh=domain_name_zh,
        domain_bbox=domain_bbox,
        domain_center=domain_center,
        source_flow_domains_config=source_flow_domains_config,
        source_flow_domains_config_sha256=source_flow_domains_hash,
        source_flow_domains_path=source_flow_domains_path,
        years=years,
        months=months,
        required_status=required_status,
        required_cache_kinds=frozenset(str(item) for item in kinds_raw),
        required_cache_schema_major=schema_major,
        expected_timestep_hours=timestep_hours,
        maximum_source_gap_hours=maximum_gap,
        time_axis_policy=str(time_axis_policy),
        fixed_depths_m=fixed_depths,
        vertical_weights_m=vertical_weights,
        minimum_feature_valid_fraction=minimum_feature_valid_fraction,
        minimum_retained_time_fraction=minimum_retained_time_fraction,
        minimum_cells_per_velocity_level=minimum_cells_per_velocity_level,
        minimum_eta_cells=minimum_eta_cells,
        requested_mode_count=requested_mode_count,
        minimum_reported_mode_count=minimum_reported_mode_count,
        dense_memory_limit_bytes=dense_memory_limit,
        operator_time_block_rows=operator_time_block_rows,
        propack_maxiter=propack_maxiter,
        propack_max_attempts=propack_max_attempts,
        residual_tolerance=residual_tolerance,
        random_seed=random_seed,
        native_io_workers=native_io_workers,
        native_block_read_strategy=str(native_block_read_strategy),
        linear_algebra_threads=linear_algebra_threads,
        native_time_block_size=native_time_block_size,
        figure_mode_count=figure_mode_count,
        figure_formats=tuple(str(item) for item in formats_raw),
        figure_dpi=_as_positive_int(figures.get("raster_dpi"), "figures.raster_dpi"),
        max_quiver_arrows_per_axis=_as_positive_int(figures.get("max_quiver_arrows_per_axis"), "figures.max_quiver_arrows_per_axis"),
        figure_land_overlay_path=coastline_path,
        figure_land_overlay_logical_path=(
            str(coastline_logical_path) if coastline_logical_path is not None else None
        ),
        figure_land_overlay_sha256=(str(coastline_sha256) if coastline_sha256 is not None else None),
    )


def _load_full_domain_grid(
    surface_root: Path,
    native_root: Path,
    config: WaterColumnConfig,
) -> GridContext:
    """讀取完整 flow domain 格網與 paired native node 對應。

    flow domain bbox 與 grid cell center 不一定位元相同，因此 geometry mask 仍以封閉 bbox
    明確建立；這保護未來上游在 bbox 邊緣增列 I/O buffer 格點時，不會讓下游擅自把它們
    放入本次完整 domain SVD。
    """

    surface_grid = surface_root / config.domain_id / "grid"
    native_grid = native_root / config.domain_id / "grid"
    _require(surface_grid.is_dir(), f"找不到 surface grid: {surface_grid}")
    _require(native_grid.is_dir(), f"找不到 native grid: {native_grid}")
    metadata = _read_json_object(surface_grid / "metadata.json")
    metadata_domain = metadata.get("domain")
    _require(isinstance(metadata_domain, dict) and metadata_domain.get("domain_id") == config.domain_id, "surface grid metadata domain 與設定不一致")

    def load_array(directory: Path, filename: str) -> np.ndarray:
        path = directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"grid 缺少必要欄位: {path}")
        return np.load(path, mmap_mode="r", allow_pickle=False)

    lon = np.asarray(load_array(surface_grid, "lon.npy"), dtype=np.float64)
    lat = np.asarray(load_array(surface_grid, "lat.npy"), dtype=np.float64)
    area = np.asarray(load_array(surface_grid, "cell_area_m2.npy"), dtype=np.float64)
    bathymetry = np.asarray(load_array(surface_grid, "bathymetry_m.npy"), dtype=np.float64)
    static = np.asarray(load_array(surface_grid, "mask_static.npy"), dtype=bool)
    vertices = np.asarray(load_array(surface_grid, "source_vertices.npy"), dtype=np.int64)
    weights = np.asarray(load_array(surface_grid, "source_weights.npy"), dtype=np.float64)
    source_global = np.asarray(load_array(native_grid, "source_node_global_index.npy"), dtype=np.int64)
    shape = (lat.size, lon.size)
    _require(lon.ndim == 1 and lat.ndim == 1 and np.all(np.diff(lon) > 0) and np.all(np.diff(lat) > 0), "完整 flow domain lon/lat 必須是一維嚴格遞增軸")
    _require(area.shape == shape and bathymetry.shape == shape and static.shape == shape, "surface 靜態格網欄位必須是 (lat,lon)")
    _require(vertices.shape == (*shape, 3) and weights.shape == vertices.shape, "source_vertices/source_weights 必須是 (lat,lon,3)")
    _require(np.all(np.isfinite(area[static])) and np.all(area[static] > 0.0), "靜態海域 cell_area_m2 必須為有限正值")
    lon_min, lon_max, lat_min, lat_max = config.domain_bbox
    tolerance = 1.0e-10
    geometry = ((lat[:, None] >= lat_min - tolerance) & (lat[:, None] <= lat_max + tolerance) & (lon[None, :] >= lon_min - tolerance) & (lon[None, :] <= lon_max + tolerance))
    _require(np.any(geometry), "完整 flow domain bbox 沒有任何規則格點")
    supported = np.all(vertices >= 0, axis=-1) & np.all(np.isfinite(weights), axis=-1)
    source_indices = vertices[vertices >= 0]
    _require(source_indices.size > 0 and int(np.max(source_indices)) < source_global.size, "surface source_vertices 與 native source node 軸不相容")
    selected_nodes = np.unique(source_indices)
    selected_node_run_count = int(np.count_nonzero(np.diff(selected_nodes) != 1) + 1)
    local_lookup = np.full(source_global.size, -1, dtype=np.int64)
    local_lookup[selected_nodes] = np.arange(selected_nodes.size, dtype=np.int64)
    local_vertices = np.full(vertices.shape, -1, dtype=np.int64)
    valid_vertices = vertices >= 0
    local_vertices[valid_vertices] = local_lookup[vertices[valid_vertices]]
    return GridContext(
        lon=lon,
        lat=lat,
        cell_area_m2=area,
        bathymetry_m=bathymetry,
        static_mask=static,
        geometry_mask=geometry,
        supported_mask=supported,
        selected_nodes=selected_nodes,
        source_node_count=int(source_global.size),
        selected_node_run_count=selected_node_run_count,
        local_vertices=local_vertices,
        source_weights=weights,
    )


def _validate_month_metadata(
    metadata: dict[str, Any],
    config: WaterColumnConfig,
    month_id: str,
    *,
    allow_partial_months: bool,
    allow_trial: bool,
) -> str:
    """驗證單月 paired cache 的狀態、schema 與 domain，回傳允許的 cache kind。

    ``--allow-partial-months`` 與 ``--allow-trial`` 都必須由操作者顯式給定；它們只放寬
    cache 發布狀態，絕不填補缺日或略過後續 UTC／缺值檢查。這使 SERVER 的全部可得兩年
    run 與本機單日 trial 可以使用同一程式，而不會混淆科學狀態。
    """

    domain = metadata.get("domain")
    _require(isinstance(domain, dict) and domain.get("domain_id") == config.domain_id, f"{month_id} metadata domain 與設定不一致")
    _require(metadata.get("month") == month_id, f"{month_id} metadata.month 不一致")
    schema = metadata.get("cache_schema_version")
    _require(isinstance(schema, str) and schema.split(".", 1)[0] == str(config.required_cache_schema_major), f"{month_id} cache schema 必須是 {config.required_cache_schema_major}.x")
    status = metadata.get("status")
    cache_kind = metadata.get("cache_kind")
    if allow_trial and status == "trial_ready" and cache_kind == "trial_partial_month":
        return str(cache_kind)
    _require(status == config.required_status, f"{month_id} status 必須是 {config.required_status}，實際為 {status!r}")
    if cache_kind in config.required_cache_kinds:
        return str(cache_kind)
    if allow_partial_months and cache_kind == "standard_partial_month":
        return str(cache_kind)
    raise ValueError(
        f"{month_id} cache_kind={cache_kind!r} 不在允許集合；"
        "若確認需使用缺日月份，請明確加 --allow-partial-months"
    )


def _open_month_array(directory: Path, month_id: str, filename: str) -> np.ndarray:
    """以唯讀 memory-map 開啟必要月份欄位，保留可定位的錯誤訊息。"""

    path = directory / filename
    if not path.is_file():
        raise FileNotFoundError(f"{month_id} 缺少必要欄位: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _canonicalize_time_axis(
    source_time_utc_ns: np.ndarray,
    config: WaterColumnConfig,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """建立嚴格遞增且每個 UTC 唯一的分析時間軸。

    全部可得資料可能因跨月來源檔標籤問題出現倒序或重複。設定選擇
    ``sort_and_deduplicate_prefer_last`` 時，本函式以 stable sort 對相同 UTC 保留來源順序
    最後一筆；它只丟棄重複樣本、不建立新時次，且會由同一 retained index 同步控制所有
    u/v/eta 寫入。嚴格政策則拒絕任何非遞增時間軸。
    """

    source_time = np.asarray(source_time_utc_ns, dtype=np.int64)
    _require(source_time.ndim == 1 and source_time.size >= 2, "跨月 time_utc_ns 必須是一維且至少兩筆")
    original = np.arange(source_time.size, dtype=np.int64)
    if config.time_axis_policy == "reject":
        _require(np.all(np.diff(source_time) > 0), "跨月份 UTC 時軸有倒序或重複時次")
        return source_time, original, 0, 0
    chronological_order = np.argsort(source_time, kind="stable")
    sorted_time = source_time[chronological_order]
    group_ends = np.flatnonzero(np.r_[np.diff(sorted_time) != 0, True])
    retained = chronological_order[group_ends]
    canonical = sorted_time[group_ends]
    _require(np.all(np.diff(canonical) > 0), "時間 canonicalization 後 UTC 軸仍非嚴格遞增")
    return (
        canonical,
        retained,
        int(np.count_nonzero(chronological_order != original)),
        int(source_time.size - canonical.size),
    )


def _time_axis_statistics(
    time_utc_ns: np.ndarray,
    config: WaterColumnConfig,
) -> tuple[float, float, int]:
    """驗證逐時取樣節奏，並量化缺日造成的來源時間斷點。"""

    _require(time_utc_ns.size >= 2, "SVD 至少需要兩個 UTC 時次")
    diff_hours = np.diff(time_utc_ns).astype(np.float64) / float(NANOSECONDS_PER_HOUR)
    median_hours = float(np.median(diff_hours))
    maximum_hours = float(np.max(diff_hours))
    tolerance = max(0.01, config.expected_timestep_hours * 0.01)
    _require(abs(median_hours - config.expected_timestep_hours) <= tolerance, f"時間軸中位步長 {median_hours:.6g} 小時，與設定 {config.expected_timestep_hours:.6g} 小時不一致")
    gap_break_count = int(np.count_nonzero(diff_hours > config.expected_timestep_hours + tolerance))
    if config.maximum_source_gap_hours is not None:
        _require(maximum_hours <= config.maximum_source_gap_hours, f"最大來源時間缺口 {maximum_hours:.6g} 小時超過設定 {config.maximum_source_gap_hours:.6g} 小時")
    return median_hours, maximum_hours, gap_break_count


def _discover_timeline(
    surface_root: Path,
    native_root: Path,
    config: WaterColumnConfig,
    grid: GridContext,
    *,
    allow_partial_months: bool,
    allow_trial: bool,
) -> Timeline:
    """唯讀驗證全部 paired 月份並建立 canonical UTC 軸。

    此步驟只開啟 NPY metadata/header 與 time 軸，不 materialize 動態流場。它在進行數十
    GiB 暫存矩陣寫入前先攔截 surface/native 不配對、grid shape 不符或 time 不一致等問題，
    讓 SERVER 正式運算的失敗成本保持可控。
    """

    descriptors_unmapped: list[tuple[int, int, str, dict[str, Any], dict[str, Any], str, np.ndarray]] = []
    source_time_parts: list[np.ndarray] = []
    grid_shape = grid.static_mask.shape
    for year in config.years:
        for month in config.months:
            month_id = f"{year}{month:02d}"
            surface_month = surface_root / config.domain_id / "months" / month_id
            native_month = native_root / config.domain_id / "months" / month_id
            _require(surface_month.is_dir() and native_month.is_dir(), f"{month_id} 缺少 paired surface/native 月份目錄")
            surface_metadata = _read_json_object(surface_month / "metadata.json")
            native_metadata = _read_json_object(native_month / "metadata.json")
            surface_kind = _validate_month_metadata(surface_metadata, config, month_id, allow_partial_months=allow_partial_months, allow_trial=allow_trial)
            native_kind = _validate_month_metadata(native_metadata, config, month_id, allow_partial_months=allow_partial_months, allow_trial=allow_trial)
            _require(surface_kind == native_kind, f"{month_id} paired cache_kind 不一致")
            _require(surface_metadata.get("config_hash") == native_metadata.get("config_hash"), f"{month_id} paired config_hash 不一致")
            surface_time = _open_month_array(surface_month, month_id, "time_utc_ns.npy")
            native_time = _open_month_array(native_month, month_id, "time_utc_ns.npy")
            _require(surface_time.dtype == np.int64 and native_time.dtype == np.int64 and surface_time.ndim == native_time.ndim == 1 and surface_time.size > 0, f"{month_id} paired time_utc_ns 必須是非空 int64 一維軸")
            _require(np.array_equal(surface_time, native_time), f"{month_id} paired surface/native time_utc_ns 必須逐值相同")
            _require(np.all(np.diff(surface_time) > 0), f"{month_id} 月內 time_utc_ns 必須嚴格遞增")
            expected_surface_shape = (surface_time.size, *grid_shape)
            for filename in ("u_surface_mps.npy", "v_surface_mps.npy", "eta_m.npy"):
                array = _open_month_array(surface_month, month_id, filename)
                _require(array.shape == expected_surface_shape and np.issubdtype(array.dtype, np.floating), f"{month_id} {filename} 必須是浮點 (time,lat,lon) 且與完整 flow grid 對齊")
            valid = _open_month_array(surface_month, month_id, "valid_mask_surface.npy")
            _require(valid.dtype == np.bool_ and valid.shape == expected_surface_shape, f"{month_id} valid_mask_surface.npy 必須與完整 flow grid 對齊")
            hvel = _open_month_array(native_month, month_id, "hvel.npy")
            zcor = _open_month_array(native_month, month_id, "zcor.npy")
            _require(hvel.ndim == 4 and hvel.shape[0] == surface_time.size and hvel.shape[-1] >= 2 and np.issubdtype(hvel.dtype, np.floating), f"{month_id} hvel 必須是浮點 (time,node,layer,component>=2)")
            _require(zcor.shape == hvel.shape[:3] and np.issubdtype(zcor.dtype, np.floating), f"{month_id} zcor 必須對齊 hvel 的前三維")
            _require(int(grid.selected_nodes[-1]) < hvel.shape[1], f"{month_id} selected native node 超出 hvel node 軸")
            descriptors_unmapped.append((year, month, month_id, surface_metadata, native_metadata, surface_kind, np.asarray(surface_time, dtype=np.int64)))
            source_time_parts.append(np.asarray(surface_time, dtype=np.int64))

    source_time = np.concatenate(source_time_parts)
    canonical_time, retained_source_indices, reordered_count, dropped_count = _canonicalize_time_axis(source_time, config)
    median_hours, maximum_hours, gap_break_count = _time_axis_statistics(canonical_time, config)
    source_to_canonical = np.full(source_time.size, -1, dtype=np.int64)
    source_to_canonical[retained_source_indices] = np.arange(canonical_time.size, dtype=np.int64)
    descriptors: list[MonthDescriptor] = []
    offset = 0
    for year, month, month_id, surface_metadata, native_metadata, cache_kind, month_time in descriptors_unmapped:
        stop = offset + month_time.size
        descriptors.append(
            MonthDescriptor(
                year=year,
                month=month,
                month_id=month_id,
                source_metadata=surface_metadata,
                native_metadata=native_metadata,
                cache_kind=cache_kind,
                source_start=offset,
                source_stop=stop,
                canonical_rows=source_to_canonical[offset:stop].copy(),
            )
        )
        offset = stop
    return Timeline(
        time_utc_ns=canonical_time,
        descriptors=tuple(descriptors),
        source_time_count=int(source_time.size),
        reordered_time_step_count=reordered_count,
        dropped_duplicate_time_step_count=dropped_count,
        median_timestep_hours=median_hours,
        maximum_gap_hours=maximum_hours,
        gap_break_count=gap_break_count,
    )


def _build_candidate_feature_layout(
    grid: GridContext,
    config: WaterColumnConfig,
) -> CandidateFeatureLayout:
    """依 bathymetry 與插值支撐預先建立一趟 native I/O 可寫入的暫存欄位。

    水下固定深度的候選格點先要求靜態海域、完整 domain 幾何、重心內插支撐與 bathymetry
    不淺於目標深度。真實 ``zcor`` 仍可能因潮位、濕乾或缺值無法包夾；那些情況會在讀完
    所有時間後以實際有限率篩除，這個先驗篩選只避免明知海床以上的 feature 佔用磁碟。
    """

    base = grid.geometry_mask & grid.static_mask
    rows_by_level: list[np.ndarray] = []
    cols_by_level: list[np.ndarray] = []
    u_slices: list[slice] = []
    v_slices: list[slice] = []
    sqrt_weights: list[np.ndarray] = []
    groups: list[np.ndarray] = []
    offset = 0
    for level_index, depth_m in enumerate(VELOCITY_LEVEL_DEPTHS_M):
        if level_index == 0:
            candidate = base
        else:
            candidate = base & grid.supported_mask & np.isfinite(grid.bathymetry_m) & (grid.bathymetry_m >= depth_m)
        rows, cols = np.where(candidate)
        _require(rows.size > 0, f"{VELOCITY_LEVEL_LABELS_ZH[level_index]} 沒有任何靜態候選格點")
        rows_by_level.append(rows.astype(np.int64))
        cols_by_level.append(cols.astype(np.int64))
        count = int(rows.size)
        u_slices.append(slice(offset, offset + count))
        offset += count
        v_slices.append(slice(offset, offset + count))
        offset += count
        metric = np.sqrt(grid.cell_area_m2[rows, cols] * config.vertical_weights_m[level_index])
        sqrt_weights.extend((metric, metric))
        groups.extend((np.zeros(count, dtype=np.int8), np.zeros(count, dtype=np.int8)))
    eta_rows, eta_cols = np.where(base)
    _require(eta_rows.size > 0, "eta 沒有任何靜態候選格點")
    eta_slice = slice(offset, offset + int(eta_rows.size))
    sqrt_weights.append(np.sqrt(grid.cell_area_m2[eta_rows, eta_cols]))
    groups.append(np.ones(eta_rows.size, dtype=np.int8))
    return CandidateFeatureLayout(
        level_rows=tuple(rows_by_level),
        level_cols=tuple(cols_by_level),
        level_u_slices=tuple(u_slices),
        level_v_slices=tuple(v_slices),
        eta_rows=eta_rows.astype(np.int64),
        eta_cols=eta_cols.astype(np.int64),
        eta_slice=eta_slice,
        sqrt_metric_weight=np.concatenate(sqrt_weights).astype(np.float64, copy=False),
        feature_group=np.concatenate(groups).astype(np.int8, copy=False),
    )


def _read_one_month_block(
    surface_root: Path,
    native_root: Path,
    config: WaterColumnConfig,
    grid: GridContext,
    descriptor: MonthDescriptor,
    time_slice: slice,
) -> tuple[MonthDescriptor, slice, np.ndarray, dict[str, np.ndarray]]:
    """讀取一個獨立 paired 時間區塊，供單執行緒或受控 thread pool 共用。

    每個工作只開啟自己月份的唯讀 memory-map，並 materialize 一個小於
    ``native_time_block_size`` 的 selected native-node 區塊。這讓不同月份／時段可平行讀取，
    但沒有任何 worker 寫入科學輸出；主執行緒會依唯一的 ``canonical_rows`` 寫入 raw
    feature matrix，避免多執行緒碰撞或改變 UTC 對應。固定深度五組 ``u/v`` 共用同一份
    ``hvel/zcor`` 小塊，故不會為各深度重複讀取 native 3D 陣列。
    """

    grid_shape = grid.static_mask.shape
    surface_month = surface_root / config.domain_id / "months" / descriptor.month_id
    native_month = native_root / config.domain_id / "months" / descriptor.month_id
    u_surface = _open_month_array(surface_month, descriptor.month_id, "u_surface_mps.npy")
    v_surface = _open_month_array(surface_month, descriptor.month_id, "v_surface_mps.npy")
    eta = _open_month_array(surface_month, descriptor.month_id, "eta_m.npy")
    valid_surface = _open_month_array(surface_month, descriptor.month_id, "valid_mask_surface.npy")
    hvel = _open_month_array(native_month, descriptor.month_id, "hvel.npy")
    zcor = _open_month_array(native_month, descriptor.month_id, "zcor.npy")
    time_count = descriptor.canonical_rows.size
    expected_shape = (time_count, *grid_shape)
    _require(u_surface.shape == v_surface.shape == eta.shape == valid_surface.shape == expected_shape, f"{descriptor.month_id} surface 動態欄位在讀取期 shape 改變")
    _require(hvel.shape[0] == zcor.shape[0] == time_count, f"{descriptor.month_id} native 動態欄位在讀取期 time 軸改變")
    canonical_rows = descriptor.canonical_rows[time_slice]
    _require(np.any(canonical_rows >= 0), "排程器不應提交全部被跨月去重捨棄的時間區塊")
    surface_u = np.asarray(u_surface[time_slice], dtype=np.float64)
    surface_v = np.asarray(v_surface[time_slice], dtype=np.float64)
    eta_values = np.asarray(eta[time_slice], dtype=np.float64)
    surface_valid = np.asarray(valid_surface[time_slice], dtype=bool)
    # 此分支由設定顯式鎖定，而非按當下 CPU／RAM 或來源檔大小猜測。後灣兩年 NFS 實測顯示，
    # 只 materialize 6,984 個插值必要 node 比完整 24,447-node 軸更快；兩者均會得到相同
    # selected_hvel/selected_zcor，差異僅在讀取未使用 source node 的 I/O 成本。
    if config.native_block_read_strategy == "contiguous_full_source_axis_then_select":
        all_node_hvel = np.asarray(hvel[time_slice, :, :, :2], dtype=np.float64)
        all_node_zcor = np.asarray(zcor[time_slice, :, :], dtype=np.float64)
        selected_hvel = np.asarray(all_node_hvel[:, grid.selected_nodes, :, :], dtype=np.float64)
        selected_zcor = np.asarray(all_node_zcor[:, grid.selected_nodes, :], dtype=np.float64)
        # advanced-index 結果已是獨立陣列；立即釋放完整節點軸，將每一 worker 的常駐 RSS
        # 控制在固定深度內插實際需要的子集，而非整個 flow-domain native block。
        del all_node_hvel, all_node_zcor
    else:
        selected_hvel = np.asarray(hvel[time_slice, grid.selected_nodes, :, :2], dtype=np.float64)
        selected_zcor = np.asarray(zcor[time_slice, grid.selected_nodes, :], dtype=np.float64)
    fields: dict[str, np.ndarray] = {
        "surface_u": np.where(surface_valid, surface_u, np.nan),
        "surface_v": np.where(surface_valid, surface_v, np.nan),
        "eta": eta_values,
    }
    for depth_m, level_id in zip(config.fixed_depths_m, VELOCITY_LEVEL_IDS[1:], strict=True):
        node_u, node_v, _bracket_span = interpolate_velocity_to_target_z(
            selected_hvel,
            selected_zcor,
            -depth_m,
        )
        fields[f"{level_id}_u"] = _horizontal_barycentric_interpolate(
            node_u,
            grid.local_vertices,
            grid.source_weights,
        )
        fields[f"{level_id}_v"] = _horizontal_barycentric_interpolate(
            node_v,
            grid.local_vertices,
            grid.source_weights,
        )
    return descriptor, time_slice, canonical_rows, fields


def _iter_month_blocks(
    surface_root: Path,
    native_root: Path,
    config: WaterColumnConfig,
    grid: GridContext,
    timeline: Timeline,
) -> Iterator[tuple[MonthDescriptor, slice, np.ndarray, dict[str, np.ndarray]]]:
    """以可重現 UTC 對應平行讀取 paired 區塊，產生未篩選的表層／固定深度場。

    ``native_io_workers=1`` 時採序列讀取，方便小型開發與逐步除錯；大於一時以 thread pool
    平行讀取不同月／時段的 memory-map 資料。每個 future 的結果都由主執行緒寫到不重複的
    canonical time row，所以完成順序不影響矩陣內容。in-flight 數量限制在 worker 的兩倍，
    讓大型 ``hvel/zcor`` block 不會因任務佇列累積而用盡 SERVER RAM。
    """

    task_specs: list[tuple[MonthDescriptor, slice]] = []
    for descriptor in timeline.descriptors:
        time_count = descriptor.canonical_rows.size
        for start in range(0, time_count, config.native_time_block_size):
            stop = min(time_count, start + config.native_time_block_size)
            time_slice = slice(start, stop)
            # 去重掉的來源樣本無須花費昂貴 native 3D I/O；同月其餘時次仍保持原順序。
            if np.any(descriptor.canonical_rows[time_slice] >= 0):
                task_specs.append((descriptor, time_slice))
    _require(bool(task_specs), "沒有可讀取的 paired native/surface 時間區塊")
    worker_count = min(config.native_io_workers, os.cpu_count() or 1, len(task_specs))
    if worker_count == 1:
        for descriptor, time_slice in task_specs:
            yield _read_one_month_block(surface_root, native_root, config, grid, descriptor, time_slice)
        return

    task_iterator = iter(task_specs)
    max_in_flight = min(len(task_specs), worker_count * 2)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocm-native-io") as executor:
        pending = set()
        for _ in range(max_in_flight):
            descriptor, time_slice = next(task_iterator)
            pending.add(executor.submit(_read_one_month_block, surface_root, native_root, config, grid, descriptor, time_slice))
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                yield future.result()
                try:
                    descriptor, time_slice = next(task_iterator)
                except StopIteration:
                    continue
                pending.add(executor.submit(_read_one_month_block, surface_root, native_root, config, grid, descriptor, time_slice))


def _write_raw_feature_matrix(
    raw_matrix_path: Path,
    *,
    surface_root: Path,
    native_root: Path,
    config: WaterColumnConfig,
    grid: GridContext,
    timeline: Timeline,
    layout: CandidateFeatureLayout,
) -> None:
    """以一趟 paired native/surface I/O 建立 float32 原始 feature memory-map。

    暫存檔只保存已依 bathymetry 排除明顯不可能深度的欄位；資料仍保留原始 NaN，尚未扣除
    平均值、加權或正規化。這讓後續有效率判定能依真實 ``zcor`` 結果進行，而不是以假設
    的 bathymetry 代替資料品質，亦避免為了選遮罩重讀數十個月的 native 大陣列。
    """

    raw = np.memmap(
        raw_matrix_path,
        dtype=np.float32,
        mode="w+",
        shape=(timeline.time_utc_ns.size, layout.feature_count),
    )
    # 空檔預設為 0；明確初始化 NaN 是缺值科學契約的一部分，否則未支撐格點會被誤判成
    # 真正靜水並污染 SVD。分塊設定可降低初始化時的瞬時 RSS。
    rows_per_chunk = max(1, min(256, timeline.time_utc_ns.size))
    for start in range(0, timeline.time_utc_ns.size, rows_per_chunk):
        raw[start : start + rows_per_chunk] = np.nan
    raw.flush()
    try:
        for _descriptor, _time_slice, canonical_rows, fields in _iter_month_blocks(
            surface_root,
            native_root,
            config,
            grid,
            timeline,
        ):
            retained = canonical_rows >= 0
            target_rows = canonical_rows[retained]
            if target_rows.size == 0:
                continue
            for level_index, level_id in enumerate(VELOCITY_LEVEL_IDS):
                if level_index == 0:
                    u_values = fields["surface_u"]
                    v_values = fields["surface_v"]
                else:
                    u_values = fields[f"{level_id}_u"]
                    v_values = fields[f"{level_id}_v"]
                rows = layout.level_rows[level_index]
                cols = layout.level_cols[level_index]
                raw[target_rows, layout.level_u_slices[level_index]] = u_values[retained][:, rows, cols]
                raw[target_rows, layout.level_v_slices[level_index]] = v_values[retained][:, rows, cols]
            raw[target_rows, layout.eta_slice] = fields["eta"][retained][:, layout.eta_rows, layout.eta_cols]
        raw.flush()
    finally:
        del raw


def _select_feature_layout(
    raw_matrix_path: Path,
    *,
    time_count: int,
    candidate: CandidateFeatureLayout,
    grid: GridContext,
    config: WaterColumnConfig,
) -> tuple[SelectedFeatureLayout, np.ndarray]:
    """依實際有限率選擇 paired u/v 與 eta 欄位，並建立完整時間交集。

    首先依設定門檻選取 feature；若任一時次因少量濕乾／缺值而無法形成完整矩陣，則改採
    完全有限 feature 的保守 fallback。這個 fallback 會犧牲容易缺值的格點而保留時間樣本，
    但永遠不插補，也不將 NaN 轉成 0。所有選擇結果與政策會發布至 metadata 及深度遮罩。
    """

    raw = np.memmap(raw_matrix_path, dtype=np.float32, mode="r", shape=(time_count, candidate.feature_count))
    try:
        finite_count = np.zeros(candidate.feature_count, dtype=np.int64)
        block_rows = max(1, config.operator_time_block_rows)
        for start in range(0, time_count, block_rows):
            finite_count += np.count_nonzero(np.isfinite(raw[start : start + block_rows]), axis=0)
        valid_fraction = finite_count.astype(np.float64) / float(time_count)

        def choose_columns(required_fraction: float) -> tuple[list[np.ndarray], np.ndarray]:
            """依指定有效率選出每層 paired u/v 與 eta 的 raw 欄索引。"""

            level_columns: list[np.ndarray] = []
            for level_index in range(len(VELOCITY_LEVEL_IDS)):
                u_columns = np.arange(candidate.level_u_slices[level_index].start, candidate.level_u_slices[level_index].stop, dtype=np.int64)
                v_columns = np.arange(candidate.level_v_slices[level_index].start, candidate.level_v_slices[level_index].stop, dtype=np.int64)
                paired = (valid_fraction[u_columns] >= required_fraction) & (valid_fraction[v_columns] >= required_fraction)
                level_columns.append(np.flatnonzero(paired).astype(np.int64))
            eta_columns = np.arange(candidate.eta_slice.start, candidate.eta_slice.stop, dtype=np.int64)
            eta_selected = np.flatnonzero(valid_fraction[eta_columns] >= required_fraction).astype(np.int64)
            return level_columns, eta_selected

        def flatten_columns(level_offsets: Sequence[np.ndarray], eta_offsets: np.ndarray) -> np.ndarray:
            """將局部 cell offset 轉回 raw feature 欄索引，維持 u/v 配對順序。"""

            parts: list[np.ndarray] = []
            for level_index, offsets in enumerate(level_offsets):
                parts.append(candidate.level_u_slices[level_index].start + offsets)
                parts.append(candidate.level_v_slices[level_index].start + offsets)
            parts.append(candidate.eta_slice.start + eta_offsets)
            return np.concatenate(parts).astype(np.int64, copy=False)

        level_offsets, eta_offsets = choose_columns(config.minimum_feature_valid_fraction)
        selected_raw = flatten_columns(level_offsets, eta_offsets)
        complete_time = np.zeros(time_count, dtype=bool)
        if selected_raw.size:
            for start in range(0, time_count, block_rows):
                block = raw[start : start + block_rows, selected_raw]
                complete_time[start : start + block.shape[0]] = np.all(np.isfinite(block), axis=1)
        retained_fraction = float(np.mean(complete_time))
        policy = "configured_minimum_feature_valid_fraction"
        # 完整直接 SVD 不容許矩陣內有 NaN。若採設定門檻的 feature 組合導致太多整列被
        # 排除，退至 100% finite feature；此行為是預先定義的遮罩收縮，而非數值插補。
        if retained_fraction < config.minimum_retained_time_fraction:
            level_offsets, eta_offsets = choose_columns(1.0)
            selected_raw = flatten_columns(level_offsets, eta_offsets)
            complete_time.fill(False)
            if selected_raw.size:
                for start in range(0, time_count, block_rows):
                    block = raw[start : start + block_rows, selected_raw]
                    complete_time[start : start + block.shape[0]] = np.all(np.isfinite(block), axis=1)
            retained_fraction = float(np.mean(complete_time))
            policy = "complete_feature_fallback_after_time_intersection_loss"
        _require(retained_fraction >= config.minimum_retained_time_fraction, f"無插補的共同完整時次僅保留 {retained_fraction:.3%}，低於設定 {config.minimum_retained_time_fraction:.3%}")
        _require(
            int(np.count_nonzero(complete_time)) >= config.requested_mode_count,
            "共同完整時間樣本少於設定要求的模態數，無法求取完整設定模態數",
        )
        _require(eta_offsets.size >= config.minimum_eta_cells, f"eta 有效格點僅 {eta_offsets.size}，低於門檻 {config.minimum_eta_cells}")
        for level_index, offsets in enumerate(level_offsets):
            _require(offsets.size >= config.minimum_cells_per_velocity_level, f"{VELOCITY_LEVEL_LABELS_ZH[level_index]} 有效格點僅 {offsets.size}，低於門檻 {config.minimum_cells_per_velocity_level}")

        # 這裡是主持人要求的 feature 行排列核心：eta 只放一次且位於最前，接著所有深度
        # 的 u，最後才是所有深度的 v。raw 暫存檔仍採「時間×候選欄」方便 I/O；以下只改變
        # 進入正式 A=(feature,time) 矩陣的 feature 順序，不改變任何原始值或有效格點。
        final_offset = 0
        final_u_slices: list[slice] = [slice(0, 0)] * len(VELOCITY_LEVEL_IDS)
        final_v_slices: list[slice] = [slice(0, 0)] * len(VELOCITY_LEVEL_IDS)
        selected_weight_parts: list[np.ndarray] = []
        selected_group_parts: list[np.ndarray] = []
        selected_raw_parts: list[np.ndarray] = []
        selected_rows = [candidate.level_rows[level_index][offsets] for level_index, offsets in enumerate(level_offsets)]
        selected_cols = [candidate.level_cols[level_index][offsets] for level_index, offsets in enumerate(level_offsets)]

        raw_eta = candidate.eta_slice.start + eta_offsets
        final_eta_slice = slice(final_offset, final_offset + eta_offsets.size)
        final_offset += eta_offsets.size
        selected_raw_parts.append(raw_eta)
        selected_weight_parts.append(candidate.sqrt_metric_weight[raw_eta])
        selected_group_parts.append(candidate.feature_group[raw_eta])

        # u 以表層、10、20、30、40、50 m 連續排列，讓 q(t) 的 u1...uN 直接對應白板圖。
        for level_index, offsets in enumerate(level_offsets):
            raw_u = candidate.level_u_slices[level_index].start + offsets
            count = offsets.size
            final_u_slices[level_index] = slice(final_offset, final_offset + count)
            final_offset += count
            selected_raw_parts.append(raw_u)
            selected_weight_parts.append(candidate.sqrt_metric_weight[raw_u])
            selected_group_parts.append(candidate.feature_group[raw_u])

        # v 使用與 u 相同的深度及格點順序，方便以 feature index map 一一配對 u_i/v_i。
        for level_index, offsets in enumerate(level_offsets):
            raw_v = candidate.level_v_slices[level_index].start + offsets
            count = offsets.size
            final_v_slices[level_index] = slice(final_offset, final_offset + count)
            final_offset += count
            selected_raw_parts.append(raw_v)
            selected_weight_parts.append(candidate.sqrt_metric_weight[raw_v])
            selected_group_parts.append(candidate.feature_group[raw_v])
        selected = SelectedFeatureLayout(
            level_rows=tuple(selected_rows),
            level_cols=tuple(selected_cols),
            level_u_slices=tuple(final_u_slices),
            level_v_slices=tuple(final_v_slices),
            eta_rows=candidate.eta_rows[eta_offsets],
            eta_cols=candidate.eta_cols[eta_offsets],
            eta_slice=final_eta_slice,
            selected_raw_columns=np.concatenate(selected_raw_parts).astype(np.int64, copy=False),
            sqrt_metric_weight=np.concatenate(selected_weight_parts).astype(np.float64, copy=False),
            feature_group=np.concatenate(selected_group_parts).astype(np.int8, copy=False),
            feature_selection_policy=policy,
            feature_valid_fraction=valid_fraction,
        )
        return selected, complete_time
    finally:
        del raw


def _compact_and_standardize_matrix(
    raw_matrix_path: Path,
    matrix_path: Path,
    *,
    source_time_count: int,
    selected: SelectedFeatureLayout,
    complete_time: np.ndarray,
    config: WaterColumnConfig,
) -> tuple[np.ndarray, float, float, np.ndarray, float]:
    """將時間主序 raw 暫存檔轉成主持人方向的 ``A=(feature,time)`` 加權距平矩陣。

    回傳的平均值仍是物理單位；輸出的 ``matrix_path`` 以 Fortran-order 儲存
    ``A = (raw - feature_mean) * sqrt(metric_weight) / group_rms``，其列順序由
    ``SelectedFeatureLayout`` 固定為 eta、全部 u 深度、全部 v 深度。速度群組的 metric
    是 cell area×垂向梯形厚度，eta 只使用 cell area，因此同一 SVD 能同時反映垂向流速
    與自由水面高度，卻不把 eta 偽裝成六個深度欄位。raw 檔仍以時間列寫入，只是受控的
    I/O 中間格式；正式求解器只會開啟此處產生的 feature×time 矩陣。
    """

    retained_indices = np.flatnonzero(complete_time).astype(np.int64)
    retained_count = int(retained_indices.size)
    # ``feature_valid_fraction`` 保存完整 candidate raw 軸，故可用其長度安全開啟尚未壓縮的
    # float32 檔；真正進入 SVD 的欄位仍只由 selected_raw_columns fancy-index 選取。
    raw = np.memmap(raw_matrix_path, dtype=np.float32, mode="r", shape=(source_time_count, int(selected.feature_valid_fraction.size)))
    matrix = np.memmap(
        matrix_path,
        dtype=np.float64,
        mode="w+",
        shape=(selected.feature_count, retained_count),
        order=WATER_COLUMN_MATRIX_ORDER,
    )
    try:
        # 分塊 fancy-indexing 將 canonical 完整時間與已篩選 feature 壓縮；每一列在前一步
        # 已保證有限，仍在這裡再次 assert，以防來源檔於 run 中被外部覆寫。
        block_rows = max(1, config.operator_time_block_rows)
        for output_start in range(0, retained_count, block_rows):
            output_stop = min(retained_count, output_start + block_rows)
            source_rows = retained_indices[output_start:output_stop]
            block = np.asarray(raw[source_rows[:, None], selected.selected_raw_columns[None, :]], dtype=np.float64)
            _require(np.all(np.isfinite(block)), "壓縮後矩陣仍含 NaN/Infinity；拒絕以 0 取代缺值")
            # 轉置只發生在每個時間區塊；寫入後每一欄就是某一時刻的完整 q(t)，
            # 每一列則是主持人白板矩陣的一個固定 feature。
            matrix[:, output_start:output_stop] = block.T
        matrix.flush()

        feature_mean = np.zeros(selected.feature_count, dtype=np.float64)
        for start in range(0, retained_count, block_rows):
            feature_mean += np.sum(matrix[:, start : start + block_rows], axis=1, dtype=np.float64)
        feature_mean /= float(retained_count)

        velocity_columns = np.flatnonzero(selected.feature_group == 0)
        eta_columns = np.flatnonzero(selected.feature_group == 1)
        _require(velocity_columns.size > 0 and eta_columns.size > 0, "聯合 SVD 必須同時有 u/v 與 eta feature")
        velocity_sum_squares = 0.0
        eta_sum_squares = 0.0
        for start in range(0, retained_count, block_rows):
            stop = min(retained_count, start + block_rows)
            block = np.asarray(matrix[:, start:stop], dtype=np.float64)
            block -= feature_mean[:, None]
            velocity_sum_squares += float(
                np.sum(
                    block[velocity_columns, :] ** 2
                    * selected.sqrt_metric_weight[velocity_columns][:, None] ** 2,
                    dtype=np.float64,
                )
            )
            eta_sum_squares += float(
                np.sum(
                    block[eta_columns, :] ** 2
                    * selected.sqrt_metric_weight[eta_columns][:, None] ** 2,
                    dtype=np.float64,
                )
            )
            matrix[:, start:stop] = block
        velocity_denominator = float(retained_count) * float(
            np.sum(selected.sqrt_metric_weight[velocity_columns] ** 2, dtype=np.float64)
        )
        eta_denominator = float(retained_count) * float(
            np.sum(selected.sqrt_metric_weight[eta_columns] ** 2, dtype=np.float64)
        )
        velocity_rms = math.sqrt(velocity_sum_squares / velocity_denominator)
        eta_rms = math.sqrt(eta_sum_squares / eta_denominator)
        _require(math.isfinite(velocity_rms) and velocity_rms > 0.0, "u/v 體積加權距平 RMS 為零或非有限")
        _require(math.isfinite(eta_rms) and eta_rms > 0.0, "eta 面積加權距平 RMS 為零或非有限")

        feature_scale = selected.sqrt_metric_weight.copy()
        feature_scale[velocity_columns] /= velocity_rms
        feature_scale[eta_columns] /= eta_rms
        for start in range(0, retained_count, block_rows):
            stop = min(retained_count, start + block_rows)
            matrix[:, start:stop] *= feature_scale[:, None]
        matrix.flush()
        total_sum_squares = 0.0
        for start in range(0, retained_count, block_rows):
            stop = min(retained_count, start + block_rows)
            total_sum_squares += float(np.sum(matrix[:, start:stop] ** 2, dtype=np.float64))
        _require(total_sum_squares > 0.0 and math.isfinite(total_sum_squares), "加權標準化矩陣總變異必須為正且有限")
        return feature_mean, velocity_rms, eta_rms, feature_scale, total_sum_squares
    finally:
        matrix.flush()
        del matrix
        del raw


def _estimated_dense_svd_bytes(matrix_shape: tuple[int, int]) -> int:
    """估計薄型 dense SVD 的保守工作記憶體需求。

    ``np.linalg.svd(..., full_matrices=False)`` 至少同時持有輸入、U、Vh 與 LAPACK 工作區。
    本估計刻意以三份最大矩陣大小保留工作區餘裕；低估只會導致 OS 壓力或 swap，故寧可將
    borderline case 導向磁碟 PROPACK，而不是讓完整 SERVER run 因記憶體尖峰失敗。
    """

    row_count, feature_count = matrix_shape
    rank = min(row_count, feature_count)
    matrix_bytes = row_count * feature_count * np.dtype(np.float64).itemsize
    u_bytes = row_count * rank * np.dtype(np.float64).itemsize
    vh_bytes = rank * feature_count * np.dtype(np.float64).itemsize
    return int(matrix_bytes + u_bytes + vh_bytes + 2 * max(matrix_bytes, u_bytes, vh_bytes))


def _stream_matmat(
    matrix_path: Path,
    shape: tuple[int, int],
    right: np.ndarray,
    block_rows: int,
) -> np.ndarray:
    """分塊計算主持人矩陣 ``A @ right``，供 PROPACK 與殘差驗證重用。

    ``A`` 的 shape 固定是 ``(feature,time)``，所以 right 的第一維必須是時間；每次只
    讀取一組連續時間欄，回傳 feature×mode 結果，不建立任何 normal matrix。
    """

    feature_count, time_count = shape
    right_2d = np.asarray(right, dtype=np.float64)
    was_vector = right_2d.ndim == 1
    if was_vector:
        right_2d = right_2d[:, None]
    _require(right_2d.shape[0] == time_count, "A @ right 的 time 維度不符")
    result = np.zeros((feature_count, right_2d.shape[1]), dtype=np.float64)
    matrix = np.memmap(matrix_path, dtype=np.float64, mode="r", shape=shape, order=WATER_COLUMN_MATRIX_ORDER)
    try:
        for start in range(0, time_count, block_rows):
            stop = min(time_count, start + block_rows)
            result += matrix[:, start:stop] @ right_2d[start:stop]
    finally:
        del matrix
    return result[:, 0] if was_vector else result


def _stream_rmatmat(
    matrix_path: Path,
    shape: tuple[int, int],
    left: np.ndarray,
    block_rows: int,
) -> np.ndarray:
    """分塊計算主持人矩陣 ``A.T @ left``，不建立任何 normal matrix。

    left 的第一維是 feature，回傳 time×mode；這正是主持人方向 SVD 的右側奇異向量
    所在軸，亦是殘差驗證的獨立重算路徑。
    """

    feature_count, time_count = shape
    left_2d = np.asarray(left, dtype=np.float64)
    was_vector = left_2d.ndim == 1
    if was_vector:
        left_2d = left_2d[:, None]
    _require(left_2d.shape[0] == feature_count, "A.T @ left 的 feature 維度不符")
    result = np.empty((time_count, left_2d.shape[1]), dtype=np.float64)
    matrix = np.memmap(matrix_path, dtype=np.float64, mode="r", shape=shape, order=WATER_COLUMN_MATRIX_ORDER)
    try:
        for start in range(0, time_count, block_rows):
            stop = min(time_count, start + block_rows)
            result[start:stop] = matrix[:, start:stop].T @ left_2d
    finally:
        del matrix
    return result[:, 0] if was_vector else result


def _compute_residuals(
    matrix_path: Path,
    shape: tuple[int, int],
    u: np.ndarray,
    singular_values: np.ndarray,
    vh: np.ndarray,
    block_rows: int,
) -> tuple[np.ndarray, np.ndarray, float]:
    """計算奇異三元組左右殘差與空間正交誤差，作為可發布品質證據。"""

    av = _stream_matmat(matrix_path, shape, vh.T, block_rows)
    at_u = _stream_rmatmat(matrix_path, shape, u, block_rows)
    scale = np.maximum(np.asarray(singular_values, dtype=np.float64), np.finfo(np.float64).tiny)
    left_residual = np.linalg.norm(av - u * scale[None, :], axis=0) / scale
    right_residual = np.linalg.norm(at_u - vh.T * scale[None, :], axis=0) / scale
    orthogonality_error = float(
        max(
            np.max(np.abs(u.T @ u - np.eye(u.shape[1]))),
            np.max(np.abs(vh @ vh.T - np.eye(vh.shape[0]))),
        )
    )
    return left_residual, right_residual, orthogonality_error


def _solve_direct_svd(
    matrix_path: Path,
    *,
    matrix_shape: tuple[int, int],
    config: WaterColumnConfig,
    total_sum_squares: float,
) -> DirectSvdResult:
    """對主持人方向 ``A=(feature,time)`` 選擇 dense LAPACK 或 streaming PROPACK 直接求解。

    兩條路徑均以 ``A`` 本身的奇異三元組為目標；差異只在記憶體持有方式與是否需要計算
    未使用的第 21 個以後模態。PROPACK 結果必須通過左右殘差與正交性驗證，否則自動提高
    iteration budget 重試，絕不把未收斂的向量發布成科學模態。大型 streaming 重算殘差
    另有明載的 ``1e-8`` 數值地板；若它比設定值寬鬆，metadata 會完整揭露兩者與實測值，且
    正交性仍必須通過獨立門檻。
    """

    feature_count, time_count = matrix_shape
    requested_modes = config.requested_mode_count
    _require(
        min(matrix_shape) >= requested_modes,
        f"矩陣 shape={matrix_shape} 無法求取設定的 {requested_modes} 個模態",
    )
    estimated_bytes = _estimated_dense_svd_bytes(matrix_shape)
    available_cpu_count = os.cpu_count() or 1
    threads = min(config.linear_algebra_threads, available_cpu_count)
    if estimated_bytes <= config.dense_memory_limit_bytes:
        matrix = np.memmap(matrix_path, dtype=np.float64, mode="r", shape=matrix_shape, order=WATER_COLUMN_MATRIX_ORDER)
        try:
            # 明確複製到 RAM，避免 OS 將 memory-map page fault 偽裝成可用記憶體而在 LAPACK
            # 工作區建立時突然 OOM。此分支只在保守估計低於設定 budget 時才會進入。
            dense_matrix = np.array(matrix, dtype=np.float64, copy=True)
        finally:
            del matrix
        with threadpool_limits(limits=threads):
            u_all, singular_all, vh_all = np.linalg.svd(dense_matrix, full_matrices=False)
        del dense_matrix
        rank_threshold = max(
            float(singular_all[0]) * np.finfo(np.float64).eps * max(matrix_shape),
            np.finfo(np.float64).tiny,
        )
        numerical_rank = int(np.count_nonzero(singular_all > rank_threshold))
        _require(
            numerical_rank >= requested_modes,
            f"直接 SVD 數值 rank={numerical_rank}，不足設定的 {requested_modes} 個可報告模態",
        )
        u = np.asarray(u_all[:, :requested_modes], dtype=np.float64)
        singular_values = np.asarray(singular_all[:requested_modes], dtype=np.float64)
        vh = np.asarray(vh_all[:requested_modes], dtype=np.float64)
        del u_all, singular_all, vh_all
        left_residual, right_residual, orthogonality_error = _compute_residuals(
            matrix_path,
            matrix_shape,
            u,
            singular_values,
            vh,
            config.operator_time_block_rows,
        )
        retained_error = math.sqrt(max(0.0, 1.0 - float(np.sum(singular_values**2)) / total_sum_squares))
        return DirectSvdResult(
            u=u,
            singular_values=singular_values,
            vh=vh,
            total_sum_squares=total_sum_squares,
            retained_reconstruction_error=retained_error,
            orthogonality_max_abs_error=orthogonality_error,
            left_residuals=left_residual,
            right_residuals=right_residual,
            solver_metadata={
                "strategy": "direct_dense_lapack",
                "dense_solver": "numpy.linalg.svd(full_matrices=False)",
                "matrix_shape": [feature_count, time_count],
                "matrix_orientation": WATER_COLUMN_MATRIX_ORIENTATION,
                "matrix_dtype": "float64 memory-map (Fortran-order source)",
                "estimated_dense_working_set_bytes": estimated_bytes,
                "configured_dense_memory_limit_bytes": config.dense_memory_limit_bytes,
                "linear_algebra_threads": threads,
                "numerical_rank": numerical_rank,
            },
        )

    try:
        from scipy.sparse.linalg import LinearOperator, svds
    except ImportError as error:  # pragma: no cover - dependency contract is tested by installation.
        raise RuntimeError("完整 flow domain 需要 streaming direct SVD，但 SciPy/PROPACK 未安裝") from error

    def matvec(vector: np.ndarray) -> np.ndarray:
        """提供 PROPACK 所需 ``A @ v``，每次只讀一個時間欄區塊。"""

        return _stream_matmat(matrix_path, matrix_shape, vector, config.operator_time_block_rows)

    def rmatvec(vector: np.ndarray) -> np.ndarray:
        """提供 PROPACK 所需 ``A.T @ u``，每次只讀一個時間欄區塊。"""

        return _stream_rmatmat(matrix_path, matrix_shape, vector, config.operator_time_block_rows)

    def matmat(vectors: np.ndarray) -> np.ndarray:
        """加速多向量 block 乘法，亦供 SciPy 依版本選用。"""

        return _stream_matmat(matrix_path, matrix_shape, vectors, config.operator_time_block_rows)

    def rmatmat(vectors: np.ndarray) -> np.ndarray:
        """加速轉置的多向量 block 乘法，避免逐 mode 重掃磁碟矩陣。"""

        return _stream_rmatmat(matrix_path, matrix_shape, vectors, config.operator_time_block_rows)

    operator = LinearOperator(
        shape=matrix_shape,
        matvec=matvec,
        rmatvec=rmatvec,
        matmat=matmat,
        rmatmat=rmatmat,
        dtype=np.float64,
    )
    # 此門檻只用於「獨立 streaming 重算」後的發佈驗證，不會傳給 PROPACK 或修改加權矩陣。
    # 當設定值比大型矩陣的已知浮點累加地板更嚴格時，若仍強迫以設定值拒絕，會把已收斂且
    # 高正交性的直接奇異三元組誤判為失敗，進而徒增重建兩年資料矩陣的風險。metadata 會同時
    # 保存設定值與此有效門檻，讓研究報告不能把二者混為一談。
    effective_residual_tolerance = max(
        config.residual_tolerance,
        STREAMING_DIRECT_SVD_RESIDUAL_NUMERICAL_FLOOR,
    )
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    maxiter = config.propack_maxiter
    for attempt_number in range(1, config.propack_max_attempts + 1):
        try:
            with threadpool_limits(limits=threads):
                u, singular_values, vh = svds(
                    operator,
                    k=requested_modes,
                    which="LM",
                    solver="propack",
                    tol=0.0,
                    maxiter=maxiter,
                    rng=np.random.default_rng(config.random_seed),
                )
            order = np.argsort(singular_values)[::-1]
            u = np.asarray(u[:, order], dtype=np.float64)
            singular_values = np.asarray(singular_values[order], dtype=np.float64)
            vh = np.asarray(vh[order], dtype=np.float64)
            left_residual, right_residual, orthogonality_error = _compute_residuals(
                matrix_path,
                matrix_shape,
                u,
                singular_values,
                vh,
                config.operator_time_block_rows,
            )
            maximum_residual = float(max(np.max(left_residual), np.max(right_residual)))
            residual_accepted = maximum_residual <= effective_residual_tolerance
            orthogonality_accepted = (
                orthogonality_error <= STREAMING_DIRECT_SVD_ORTHOGONALITY_TOLERANCE
            )
            if residual_accepted and orthogonality_accepted:
                status = (
                    "accepted_configured_residual_tolerance"
                    if maximum_residual <= config.residual_tolerance
                    else "accepted_streaming_numerical_residual_floor"
                )
            elif not residual_accepted:
                status = "retry_residual"
            else:
                status = "retry_orthogonality"
            attempts.append(
                {
                    "attempt": attempt_number,
                    "maxiter": maxiter,
                    "maximum_relative_residual": maximum_residual,
                    "orthogonality_max_abs_error": orthogonality_error,
                    "configured_relative_residual_tolerance": config.residual_tolerance,
                    "effective_relative_residual_tolerance": effective_residual_tolerance,
                    "orthogonality_max_abs_error_tolerance": STREAMING_DIRECT_SVD_ORTHOGONALITY_TOLERANCE,
                    "status": status,
                }
            )
            if residual_accepted and orthogonality_accepted:
                retained_error = math.sqrt(max(0.0, 1.0 - float(np.sum(singular_values**2)) / total_sum_squares))
                return DirectSvdResult(
                    u=u,
                    singular_values=singular_values,
                    vh=vh,
                    total_sum_squares=total_sum_squares,
                    retained_reconstruction_error=retained_error,
                    orthogonality_max_abs_error=orthogonality_error,
                    left_residuals=left_residual,
                    right_residuals=right_residual,
                    solver_metadata={
                        "strategy": "direct_propack_streaming",
                        "streaming_solver": "scipy.sparse.linalg.svds(solver='propack')",
                        "matrix_shape": [feature_count, time_count],
                        "matrix_orientation": WATER_COLUMN_MATRIX_ORIENTATION,
                        "matrix_storage_order": WATER_COLUMN_MATRIX_ORDER,
                        "matrix_dtype": "float64 memory-map (Fortran-order)",
                        "estimated_dense_working_set_bytes": estimated_bytes,
                        "configured_dense_memory_limit_bytes": config.dense_memory_limit_bytes,
                        "operator_time_block_rows": config.operator_time_block_rows,
                        "solver_tolerance": "0.0 (machine precision requested)",
                        "configured_relative_residual_tolerance": config.residual_tolerance,
                        "streaming_relative_residual_numerical_floor": STREAMING_DIRECT_SVD_RESIDUAL_NUMERICAL_FLOOR,
                        "acceptance_relative_residual_tolerance": effective_residual_tolerance,
                        "acceptance_orthogonality_max_abs_error_tolerance": STREAMING_DIRECT_SVD_ORTHOGONALITY_TOLERANCE,
                        "linear_algebra_threads": threads,
                        "attempts": attempts,
                    },
                )
        except Exception as error:  # SciPy exposes several version-specific convergence exceptions.
            last_error = error
            attempts.append(
                {
                    "attempt": attempt_number,
                    "maxiter": maxiter,
                    "status": "solver_exception",
                    "error": str(error),
                }
            )
        maxiter *= 2
    detail = json.dumps(attempts, ensure_ascii=False)
    raise RuntimeError(
        "PROPACK 在自動提高 iteration budget 後仍未通過直接 SVD 殘差驗證；"
        f"attempts={detail}; 不發布未收斂結果"
    ) from last_error


def _resolve_mode_signs(
    feature_modes: np.ndarray,
    pc: np.ndarray,
    selected: SelectedFeatureLayout,
    grid: GridContext,
    config: WaterColumnConfig,
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    """以 domain center 附近的表層 u/v/eta 固定每個 SVD 模態正負號。

    SVD 的正負號本質上不唯一。為使不同重跑與後續四區圖面不會任意翻轉，本函式優先採
    距 flow-domain center 最近的有效表層 u loading；若其數值接近零，依序退至表層 v、
    eta、全域最大絕對 loading。這只是顯示與重建的一致性規則，不代表中心點是觀測站或
    物理強迫位置。
    """

    signed_feature_modes = np.asarray(feature_modes, dtype=np.float64).copy()
    signed_pc = np.asarray(pc, dtype=np.float64).copy()
    center_lon, center_lat = config.domain_center

    def nearest_column(rows: np.ndarray, cols: np.ndarray, column_slice: slice) -> int | None:
        """找到某類有效 feature 中最靠近上游 domain center 的欄位。"""

        if rows.size == 0:
            return None
        longitude_distance = (grid.lon[cols] - center_lon) * math.cos(math.radians(center_lat))
        latitude_distance = grid.lat[rows] - center_lat
        return int(column_slice.start + np.argmin(longitude_distance**2 + latitude_distance**2))

    anchor_u = nearest_column(
        selected.level_rows[0],
        selected.level_cols[0],
        selected.level_u_slices[0],
    )
    anchor_v = nearest_column(
        selected.level_rows[0],
        selected.level_cols[0],
        selected.level_v_slices[0],
    )
    anchor_eta = nearest_column(selected.eta_rows, selected.eta_cols, selected.eta_slice)
    sign_sources: list[str] = []
    for mode_index in range(signed_feature_modes.shape[0]):
        candidates = (
            (anchor_u, "domain_center_surface_u_loading"),
            (anchor_v, "domain_center_surface_v_loading"),
            (anchor_eta, "domain_center_eta_loading"),
        )
        selected_anchor = next(
            (
                (index, source)
                for index, source in candidates
                if index is not None and abs(float(signed_feature_modes[mode_index, index])) > 1.0e-14
            ),
            None,
        )
        if selected_anchor is None:
            index = int(np.argmax(np.abs(signed_feature_modes[mode_index])))
            source = "largest_absolute_loading"
        else:
            index, source = selected_anchor
        if signed_feature_modes[mode_index, index] < 0.0:
            signed_feature_modes[mode_index] *= -1.0
            signed_pc[mode_index] *= -1.0
        sign_sources.append(source)
    return signed_feature_modes, signed_pc, tuple(sign_sources)


def _expand_selected_fields(
    values: np.ndarray,
    selected: SelectedFeatureLayout,
    grid_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把緊湊 feature 向量展回六層 u/v 與一次 eta 規則格網陣列。

    ``values`` 的第一維可為 mode 或單一平均場。輸出非有效 feature 一律為 NaN，使深水
    不存在、source 凸包外或資料有效率不足的位置在圖面與下游分析中保持可辨識，而非被
    誤解成靜止流或陸地。
    """

    compact = np.asarray(values, dtype=np.float64)
    add_leading_axis = compact.ndim == 1
    if add_leading_axis:
        compact = compact[None, :]
    _require(compact.ndim == 2 and compact.shape[1] == selected.feature_count, "待展開 feature 值必須是 (mode-or-one, feature)")
    u = np.full((compact.shape[0], len(VELOCITY_LEVEL_IDS), *grid_shape), np.nan, dtype=np.float64)
    v = np.full_like(u, np.nan)
    eta = np.full((compact.shape[0], *grid_shape), np.nan, dtype=np.float64)
    for level_index in range(len(VELOCITY_LEVEL_IDS)):
        u[:, level_index, selected.level_rows[level_index], selected.level_cols[level_index]] = compact[:, selected.level_u_slices[level_index]]
        v[:, level_index, selected.level_rows[level_index], selected.level_cols[level_index]] = compact[:, selected.level_v_slices[level_index]]
    eta[:, selected.eta_rows, selected.eta_cols] = compact[:, selected.eta_slice]
    if add_leading_axis:
        return u[0], v[0], eta[0]
    return u, v, eta


def _mask_from_selected_layout(
    selected: SelectedFeatureLayout,
    grid_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """建立每個流速深度及 eta 的最終 feature 遮罩，供 QC 與下游重建使用。"""

    velocity_mask = np.zeros((len(VELOCITY_LEVEL_IDS), *grid_shape), dtype=bool)
    for level_index in range(len(VELOCITY_LEVEL_IDS)):
        velocity_mask[level_index, selected.level_rows[level_index], selected.level_cols[level_index]] = True
    eta_mask = np.zeros(grid_shape, dtype=bool)
    eta_mask[selected.eta_rows, selected.eta_cols] = True
    return velocity_mask, eta_mask


def _plot_domain_name_zh(domain_name_zh: str) -> str:
    """把 flow-domain 內部名稱轉成可直接放入學術圖標題的海域名稱。

    上游 ``ocm_flow_domains.json`` 的名稱同時服務資料治理與檔案追溯，例如
    「後灣／海生館單區域 flow cache」；這個完整名稱若直接放到圖上，會把內部快取命名
    與研究區域名稱混在一起。圖面只移除 ``flow cache`` 等實作後綴，保留實際海域名稱，
    並統一補上「海域」以避免標題被誤讀成軟體或資料夾名稱。設定與 metadata 仍保存原始
    名稱，故此轉換只影響公開圖面文字，不改變分析邊界或任何數值。
    """

    cleaned = (
        str(domain_name_zh)
        .replace("單區域 flow cache", "")
        .replace("flow cache", "")
        .replace("完整 flow domain", "")
        .replace("flow domain", "")
        .replace("候選框", "")
        .replace(" SVD 核定區", "")
        .replace("SVD核定區", "")
        .strip(" ：:")
    )
    if not cleaned:
        return str(domain_name_zh).strip()
    return cleaned if cleaned.endswith("海域") else f"{cleaned}海域"


def _make_water_column_figures(
    output_dir: Path,
    *,
    config: WaterColumnConfig,
    grid: GridContext | WaterColumnFigureGrid,
    time_utc_ns: np.ndarray,
    regression_u: np.ndarray,
    regression_v: np.ndarray,
    regression_eta: np.ndarray,
    pc_standardized: np.ndarray,
    explained_variance: np.ndarray,
    velocity_mask: np.ndarray,
    eta_mask: np.ndarray,
) -> tuple[list[str], dict[str, Any]]:
    """建立可供報告後製的水柱獨立圖面資產。

    每個聯合 SVD 模態仍由六層速度、唯一 eta 與一條共同 PC 共同決定；但是圖面不把
    這些內容壓縮成複合 subplot。對每一個 mode，函式分別輸出六張獨立的速度空間圖、
    一張獨立 eta 空間圖與一張獨立 PC 時序圖；另外把解釋變異圖及七張深度／eta 遮罩 QC
    圖也各自輸出。這種「數值上聯合、圖面上拆分」的安排符合 EOF/PC 文獻中空間型態與
    時間係數配對但分開呈現的慣例，也讓後續 Word、PowerPoint 或論文排版能單獨取用任一
    深度而不必裁切一張 3×3 複合圖。

    速度空間圖的色彩是該深度的向量振幅 ``sqrt(u^2+v^2)``，黑色箭頭是同一模態、同一
    PC 標準差回歸下的 ``u/v``。eta 圖使用對稱色階顯示正負回歸幅度；PC 圖保留逐時灰線、
    逐日平均黑線與缺測斷線。所有圖均以固定有效 cell-edge 範圍、經緯度邊界刻度及可追溯
    海岸線繪製。每張正式速度主圖都內嵌其自身的向量比例尺，並另存透明比例尺，方便後續
    報告排版時保留同一物理尺度或改放到指定位置。
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as MatplotlibPath
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.ticker import FormatStrFormatter
    from matplotlib.transforms import Bbox

    # 沿用既有學術圖面字型挑選順序；SERVER 通常採 Noto Sans CJK TC，本機則可使用 macOS
    # Heiti/PingFang。字型名稱與 SHA-256 一併進 metadata，避免中文缺字或跨主機字型差異
    # 被誤當成相同圖面。若主機沒有可用 CJK 字型，helper 仍會顯示其 fallback 證據。
    report_font_name, report_font_sha256 = _resolve_report_font()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [report_font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    figures_dir = output_dir / "figures"
    report_dir = figures_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    lon_grid, lat_grid = np.meshgrid(grid.lon, grid.lat)
    step = max(1, int(math.ceil(max(grid.lon.size, grid.lat.size) / config.max_quiver_arrows_per_axis)))
    datetime_axis = np.asarray(time_utc_ns, dtype="datetime64[ns]")
    plot_name = _plot_domain_name_zh(config.domain_name_zh)
    report_layout = _academic_report_layout(ACADEMIC_REPORT_READY_V9, config.years)

    _require(grid.lon.size >= 2 and grid.lat.size >= 2, "獨立空間圖至少需要兩個經度與兩個緯度格點")
    lon_step = float(np.median(np.diff(grid.lon)))
    lat_step = float(np.median(np.diff(grid.lat)))
    _require(lon_step > 0.0 and lat_step > 0.0, "繪圖經緯度軸必須嚴格遞增")
    # 以所有最終納入的 feature 聯集決定共同圖面範圍；這能排除 full flow-domain 之外的
    # I/O buffer，也讓六個深度、eta 與所有 mode 使用完全相同的座標邊界，方便報告比較。
    valid_plot_mask = np.asarray(eta_mask | np.any(velocity_mask, axis=0), dtype=bool)
    valid_rows, valid_cols = np.where(valid_plot_mask)
    _require(valid_rows.size > 0, "六層速度與 eta 都沒有可繪製的有效格點")
    domain_lon_min, domain_lon_max, domain_lat_min, domain_lat_max = config.domain_bbox
    plot_lon_min = max(domain_lon_min, float(grid.lon[valid_cols].min()) - lon_step / 2.0)
    plot_lon_max = min(domain_lon_max, float(grid.lon[valid_cols].max()) + lon_step / 2.0)
    plot_lat_min = max(domain_lat_min, float(grid.lat[valid_rows].min()) - lat_step / 2.0)
    plot_lat_max = min(domain_lat_max, float(grid.lat[valid_rows].max()) + lat_step / 2.0)
    lon_span = plot_lon_max - plot_lon_min
    lat_span = plot_lat_max - plot_lat_min
    _require(lon_span > 0.0 and lat_span > 0.0, "有效 cell-edge 圖面範圍必須具有正面積")
    geographic_aspect = 1.0 / math.cos(math.radians(config.domain_center[1]))
    longitude_ticks = np.linspace(plot_lon_min, plot_lon_max, report_layout.map_axis_tick_count)
    latitude_ticks = np.linspace(plot_lat_min, plot_lat_max, report_layout.map_axis_tick_count)

    # 海岸線只作地理參照，不參與 feature mask、SVD 權重或任何統計計算。正式設定已鎖定
    # GeoJSON 與 SHA-256；合成測試若未提供圖資則保留空 tuple，仍可完整驗證獨立圖面契約。
    source_land_polygons: tuple[tuple[np.ndarray, ...], ...] = tuple()
    if config.figure_land_overlay_path is not None:
        source_land_polygons = _load_geojson_land_polygons(config.figure_land_overlay_path)
    plot_extent = (plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max)
    # 合成測試與某些內部 trial 設定可以刻意不提供岸線圖資；surface helper 在有圖資但
    # bbox 內無 polygon 時會拒絕，以免正式成果漏畫地理參照。此處先區分「沒有來源圖資」
    # 與「來源圖資存在但裁切後為空」：前者合法地產生無陸地 overlay，後者仍由 helper
    # 拋出錯誤，避免正式設定因路徑或 bbox 錯誤而靜默交付空岸線。
    plot_land_polygons = (
        _clip_land_polygons_to_extent(source_land_polygons, plot_extent)
        if source_land_polygons
        else tuple()
    )
    coastline_vertex_count = int(
        sum(ring.shape[0] for polygon in plot_land_polygons for ring in polygon)
    )

    report_font_name, report_font_sha256 = _resolve_report_font()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [report_font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )

    def save_report_figure(figure: Any, stem: str) -> list[str]:
        """將單一白底報告圖以設定格式輸出，並回傳相對於 run 的路徑。"""

        paths: list[str] = []
        figure.patch.set_facecolor("white")
        figure.patch.set_alpha(1.0)
        for output_format in config.figure_formats:
            path = report_dir / f"{stem}.{output_format}"
            save_kwargs: dict[str, Any] = {
                "bbox_inches": "tight",
                "pad_inches": 0.08,
                "transparent": False,
                "facecolor": "white",
            }
            if output_format == "png":
                save_kwargs["dpi"] = config.figure_dpi
            figure.savefig(path, **save_kwargs)
            relative_path = str(path.relative_to(output_dir))
            created.append(relative_path)
            paths.append(relative_path)
        return paths

    def save_vector_scale_asset(
        main_stem: str,
        vector_reference: float,
        vector_unit_label: str,
        reference_arrow_length_inches: float,
    ) -> list[str]:
        """另存透明、緊密裁切的向量比例尺，讓後製能與主圖分開排版。

        透明素材只包含黑色箭頭與文字，不含白底、色條或任何地理圖層；其箭頭長度由同一張
        主圖的 q95 顯示尺度換算而來。這保證後製者把主圖與比例尺 SVG 一起縮放時，比例尺
        不會偷偷代表另一個數值尺度。
        """

        figure, axis = plt.subplots(figsize=(1.65, 0.32))
        figure.patch.set_facecolor("none")
        figure.patch.set_alpha(0.0)
        axis.set_facecolor("none")
        axis.patch.set_alpha(0.0)
        axis.set_xlim(0.0, 1.0)
        axis.set_ylim(0.0, 1.0)
        axis.axis("off")
        axis_width_inches = axis.get_position().width * figure.get_figwidth()
        _require(axis_width_inches > 0.0, "透明向量比例尺的 axes 寬度必須為正")
        arrow_start_x = 0.02
        arrow_end_x = arrow_start_x + reference_arrow_length_inches / axis_width_inches
        _require(arrow_end_x < 0.45, "透明向量比例尺箭頭超出預留畫布")
        arrow_artist = axis.annotate(
            "",
            xy=(arrow_end_x, 0.50),
            xytext=(arrow_start_x, 0.50),
            arrowprops={
                "arrowstyle": "-|>",
                "color": "black",
                "linewidth": 1.0,
                "mutation_scale": 7.0,
                "shrinkA": 0.0,
                "shrinkB": 0.0,
            },
        )
        text_artist = axis.text(
            arrow_end_x + 0.045,
            0.50,
            f"{vector_reference:.3g} {vector_unit_label}",
            ha="left",
            va="center",
            fontsize=6.6,
            color="black",
        )
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        _require(arrow_artist.arrow_patch is not None, "透明向量比例尺缺少箭頭 artist")
        content_bbox_inches = Bbox.union(
            [
                arrow_artist.arrow_patch.get_window_extent(renderer),
                text_artist.get_window_extent(renderer),
            ]
        ).transformed(figure.dpi_scale_trans.inverted())
        padding = 0.035
        crop_bbox_inches = Bbox.from_extents(
            content_bbox_inches.x0 - padding,
            content_bbox_inches.y0 - padding,
            content_bbox_inches.x1 + padding,
            content_bbox_inches.y1 + padding,
        )
        paths: list[str] = []
        for output_format in config.figure_formats:
            path = report_dir / f"{main_stem}_vector_scale_transparent.{output_format}"
            save_kwargs: dict[str, Any] = {
                "bbox_inches": crop_bbox_inches,
                "pad_inches": 0.0,
                "transparent": True,
                "facecolor": "none",
            }
            if output_format == "png":
                save_kwargs["dpi"] = config.figure_dpi
            figure.savefig(path, **save_kwargs)
            relative_path = str(path.relative_to(output_dir))
            created.append(relative_path)
            paths.append(relative_path)
        plt.close(figure)
        return paths

    def add_land_overlay(axis: Any) -> None:
        """將暖灰陸地與深灰海岸線疊在資料圖層上方，只作地理參照。"""

        for polygon in plot_land_polygons:
            vertices: list[np.ndarray] = []
            codes: list[np.ndarray] = []
            for ring in polygon:
                ring_codes = np.full(ring.shape[0], MatplotlibPath.LINETO, dtype=np.uint8)
                ring_codes[0] = MatplotlibPath.MOVETO
                ring_codes[-1] = MatplotlibPath.CLOSEPOLY
                vertices.append(ring)
                codes.append(ring_codes)
            path = MatplotlibPath(np.vstack(vertices), np.concatenate(codes))
            axis.add_patch(
                PathPatch(
                    path,
                    facecolor="#D9D6CF",
                    edgecolor="#4A4A4A",
                    linewidth=0.7,
                    joinstyle="round",
                    capstyle="round",
                    zorder=4,
                    clip_on=True,
                )
            )

    def configure_map_axis(axis: Any, title: str) -> None:
        """套用所有獨立地圖共同的邊界、刻度、比例與不遮擋色條的標題排版。

        地圖標題只置於主地圖 axes 的上方；圖面建立時另以獨立 GridSpec 欄保留色條位置，
        因此雙行標題不會延伸到色條區。呼叫端應把模態名稱與物理層位分兩行，以免完整
        中文題名在狹長 flow-domain 圖上與色條或畫布邊界重疊。
        """

        axis.set_xlim(plot_lon_min, plot_lon_max)
        axis.set_ylim(plot_lat_min, plot_lat_max)
        axis.set_aspect(geographic_aspect, adjustable="box")
        axis.set_title(title, fontsize=14, pad=16, linespacing=1.28)
        axis.set_xlabel("經度（°E）", fontsize=11)
        axis.set_ylabel("緯度（°N）", fontsize=11)
        axis.set_xticks(longitude_ticks)
        axis.set_yticks(latitude_ticks)
        axis.xaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.yaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.tick_params(labelsize=report_layout.map_tick_label_size)
        axis.grid(color="white", linewidth=0.45, alpha=0.35)

    def make_map_figure() -> tuple[Any, Any, Any]:
        """建立主地圖與色條各自保留欄位的學術報告畫布。

        過去由 ``figure.colorbar(..., ax=axis)`` 自動縮放主 axes；長中文標題仍以整行繪製，
        會越過主圖右側並遮住色條。此處固定以 GridSpec 分出主地圖與細色條欄，並保留較高
        的上緣給雙行標題。這只改變報告版面，不改變經緯度範圍、色階、向量或任何 SVD 值。
        """

        figure_width = 10.8
        map_height = figure_width * (lat_span / lon_span) * geographic_aspect
        figure = plt.figure(figsize=(figure_width, max(7.8, map_height + 2.05)))
        grid_spec = figure.add_gridspec(
            1,
            2,
            width_ratios=(1.0, 0.047),
            left=0.105,
            right=0.900,
            bottom=0.105,
            top=0.830,
            wspace=0.145,
        )
        return figure, figure.add_subplot(grid_spec[0, 0]), figure.add_subplot(grid_spec[0, 1])

    def colorbar_for_field(
        figure: Any,
        colorbar_axis: Any,
        scalar: Any,
        limits: tuple[float, float],
        label: str,
    ) -> None:
        """在保留的色條欄建立固定五點邊界色條，避免侵入地圖標題區。"""

        colorbar = figure.colorbar(scalar, cax=colorbar_axis)
        colorbar.set_ticks(np.linspace(limits[0], limits[1], 5))
        colorbar.ax.yaxis.set_major_formatter(FormatStrFormatter("%.3g"))
        colorbar.update_ticks()
        colorbar.set_label(label, fontsize=10.5)
        colorbar.ax.tick_params(labelsize=report_layout.colorbar_tick_label_size)

    def finite_symmetric_limit(field: np.ndarray) -> float:
        """取得 eta 回歸場以零為中心的有限色階上限。"""

        finite = np.asarray(field[np.isfinite(field)], dtype=np.float64)
        _require(finite.size > 0, "eta 獨立圖至少需要一個有限回歸值")
        return max(float(np.max(np.abs(finite))), np.finfo(np.float64).eps)

    def velocity_scale(u_field: np.ndarray, v_field: np.ndarray) -> tuple[float, float, float]:
        """以每個深度的 98% 振幅與 q95 向量設定獨立圖色階及比例尺。"""

        magnitude = np.hypot(u_field, v_field)
        finite = np.asarray(magnitude[np.isfinite(magnitude)], dtype=np.float64)
        _require(finite.size > 0, "速度獨立圖至少需要一個有限向量振幅")
        positive = finite[finite > 0.0]
        reference = float(np.percentile(positive, 95.0)) if positive.size else np.finfo(np.float64).eps
        color_limit = max(float(np.percentile(finite, 98.0)), reference, np.finfo(np.float64).eps)
        quiver_scale = reference / max(0.045 * lon_span, np.finfo(np.float64).eps)
        return color_limit, reference, quiver_scale

    def add_velocity_reference_key(
        axis: Any,
        quiver_artist: Any,
        *,
        vector_reference: float,
        vector_unit_label: str,
    ) -> None:
        """將本張速度圖的 q95 向量比例尺直接嵌入座標框內。

        主圖離開檔案目錄後仍必須可單獨判讀，因此不再把比例尺只留給
        ``*_with_vector_scale`` 備用檔。版面沿用表層圖的右下角緊湊半透明底板，只列箭頭、
        數值與單位而不加「比例尺」等贅字；箭頭使用同一個 ``quiver_artist`` 的 scale，
        故標示值與圖上箭頭長度完全一致。
        """

        key_panel = Rectangle(
            (0.715, 0.022),
            0.260,
            0.060,
            transform=axis.transAxes,
            facecolor="#F8FBFC",
            edgecolor="#4A4A4A",
            linewidth=0.50,
            alpha=0.86,
            zorder=6,
            clip_on=False,
        )
        axis.add_patch(key_panel)
        axis.quiverkey(
            quiver_artist,
            X=0.810,
            Y=0.050,
            U=vector_reference,
            label=f"{vector_reference:.2f} {vector_unit_label}",
            labelpos="E",
            labelsep=0.045,
            coordinates="axes",
            color="black",
            fontproperties={"size": 7.2},
            zorder=7,
        )

    def render_vector_map(
        stem: str,
        color_field: np.ndarray,
        u_field: np.ndarray,
        v_field: np.ndarray,
        *,
        color_limits: tuple[float, float],
        vector_reference: float,
        quiver_scale: float,
        title: str,
        colorbar_label: str,
        vector_unit_label: str,
        valid_mask: np.ndarray,
    ) -> tuple[list[str], list[str]]:
        """輸出自帶向量比例尺的獨立速度圖與透明後製比例尺。"""

        figure, axis, colorbar_axis = make_map_figure()
        axis.set_facecolor("white")
        cmap = plt.get_cmap("viridis").with_extremes(bad="#E6E6E6")
        scalar = axis.pcolormesh(
            lon_grid,
            lat_grid,
            np.ma.masked_invalid(color_field),
            shading="auto",
            cmap=cmap,
            vmin=color_limits[0],
            vmax=color_limits[1],
            rasterized=True,
        )
        lon_indices = np.arange(0, grid.lon.size, step)
        lat_indices = np.arange(0, grid.lat.size, step)
        sampled_lon = lon_grid[np.ix_(lat_indices, lon_indices)]
        sampled_lat = lat_grid[np.ix_(lat_indices, lon_indices)]
        sampled_u = u_field[np.ix_(lat_indices, lon_indices)]
        sampled_v = v_field[np.ix_(lat_indices, lon_indices)]
        sampled_mask = valid_mask[np.ix_(lat_indices, lon_indices)]
        edge_margin_lon = lon_span * 0.06
        edge_margin_lat = lat_span * 0.06
        interior = (
            (sampled_lon >= plot_lon_min + edge_margin_lon)
            & (sampled_lon <= plot_lon_max - edge_margin_lon)
            & (sampled_lat >= plot_lat_min + edge_margin_lat)
            & (sampled_lat <= plot_lat_max - edge_margin_lat)
            & sampled_mask
            & np.isfinite(sampled_u)
            & np.isfinite(sampled_v)
        )
        quiver_artist = axis.quiver(
            sampled_lon,
            sampled_lat,
            np.ma.masked_where(~interior, sampled_u),
            np.ma.masked_where(~interior, sampled_v),
            color="black",
            angles="xy",
            scale_units="xy",
            scale=quiver_scale,
            pivot="mid",
            width=0.0032,
            headwidth=3.2,
            headlength=4.2,
            headaxislength=3.8,
            zorder=3,
        )
        add_land_overlay(axis)
        configure_map_axis(axis, title)
        colorbar_for_field(figure, colorbar_axis, scalar, color_limits, colorbar_label)
        add_velocity_reference_key(
            axis,
            quiver_artist,
            vector_reference=vector_reference,
            vector_unit_label=vector_unit_label,
        )
        # 先完成 GridSpec 版面計算再量測箭頭長度，透明比例尺才會與已發布主圖的 q95 箭頭一致。
        figure.canvas.draw()
        reference_arrow_length_inches = 0.045 * axis.bbox.width / figure.dpi
        report_paths = save_report_figure(figure, stem)
        plt.close(figure)
        transparent_paths = save_vector_scale_asset(
            stem,
            vector_reference,
            vector_unit_label,
            reference_arrow_length_inches,
        )
        return report_paths, transparent_paths

    def render_scalar_map(
        stem: str,
        field: np.ndarray,
        *,
        cmap_name: str,
        color_limits: tuple[float, float],
        title: str,
        colorbar_label: str,
        colorbar_ticks: np.ndarray | None = None,
        colorbar_tick_labels: tuple[str, ...] | None = None,
    ) -> list[str]:
        """輸出一張不含其他 subplot 的獨立標量空間圖。"""

        figure, axis, colorbar_axis = make_map_figure()
        axis.set_facecolor("white")
        cmap = plt.get_cmap(cmap_name).with_extremes(bad="#E6E6E6")
        scalar = axis.pcolormesh(
            lon_grid,
            lat_grid,
            np.ma.masked_invalid(field),
            shading="auto",
            cmap=cmap,
            vmin=color_limits[0],
            vmax=color_limits[1],
            rasterized=True,
        )
        add_land_overlay(axis)
        configure_map_axis(axis, title)
        colorbar_for_field(figure, colorbar_axis, scalar, color_limits, colorbar_label)
        if colorbar_ticks is not None:
            colorbar_axis.set_yticks(colorbar_ticks)
            if colorbar_tick_labels is not None:
                colorbar_axis.set_yticklabels(colorbar_tick_labels)
        paths = save_report_figure(figure, stem)
        plt.close(figure)
        return paths

    def render_pc(mode_number: int, mode_index: int, explained_percent: float) -> list[str]:
        """輸出單一 mode 的獨立標準化 PC 時序圖，不與任何空間圖共用畫布。"""

        time_datetime = datetime_axis
        report_time_start = time_datetime[0].astype("datetime64[M]")
        report_time_stop = (
            time_datetime[-1].astype("datetime64[M]")
            + np.timedelta64(1, "M")
            - np.timedelta64(1, "ns")
        )
        pc_values = np.asarray(pc_standardized[mode_index], dtype=np.float64)
        _require(np.all(np.isfinite(pc_values)), f"模態 {mode_number} 的標準化 PC 不可含 NaN")
        diff_hours = np.diff(time_utc_ns).astype(np.float64) / NANOSECONDS_PER_HOUR
        gap_after_indices = np.where(diff_hours > config.expected_timestep_hours * 1.5)[0]
        segment_starts = np.concatenate((np.array([0], dtype=int), gap_after_indices + 1))
        segment_stops = np.concatenate((gap_after_indices + 1, np.array([time_utc_ns.size], dtype=int)))
        time_days = time_datetime.astype("datetime64[D]")
        unique_days, day_inverse = np.unique(time_days, return_inverse=True)
        daily_values = np.asarray(
            [float(np.mean(pc_values[day_inverse == day_index])) for day_index in range(unique_days.size)],
            dtype=np.float64,
        )
        daily_gap_after_indices = np.where(
            np.diff(unique_days).astype("timedelta64[D]").astype(np.int64) > 1
        )[0]
        daily_segment_starts = np.concatenate((np.array([0], dtype=int), daily_gap_after_indices + 1))
        daily_segment_stops = np.concatenate((daily_gap_after_indices + 1, np.array([unique_days.size], dtype=int)))
        pc_limit = max(float(np.max(np.abs(pc_values))), 1.0)

        figure, axis = plt.subplots(figsize=(11.2, report_layout.pc_figure_height_inches))
        figure.patch.set_facecolor("white")
        axis.set_facecolor("white")
        axis.axhline(0.0, color="#666666", linewidth=0.75, alpha=0.9)
        for start, stop in zip(segment_starts, segment_stops):
            axis.plot(
                time_datetime[start:stop],
                pc_values[start:stop],
                color="#8A8A8A",
                linewidth=0.38,
                alpha=0.28,
            )
        for start, stop in zip(daily_segment_starts, daily_segment_stops):
            axis.plot(unique_days[start:stop], daily_values[start:stop], color="black", linewidth=1.15)
        axis.set_xlim(report_time_start, report_time_stop)
        axis.set_ylim(-pc_limit * 1.04, pc_limit * 1.04)
        axis.set_title(
            f"{plot_name}：模態 {mode_number} 標準化主成分時間係數 PC"
            f"（解釋變異量：{explained_percent:.2f}%）",
            fontsize=15,
            pad=12,
        )
        axis.set_xlabel("月份（UTC）", fontsize=11)
        axis.set_ylabel("標準化主成分時間係數 PC（σ）", fontsize=11)
        axis.xaxis.set_major_locator(mdates.MonthLocator(bymonth=report_layout.pc_major_tick_months))
        axis.xaxis.set_major_formatter(mdates.DateFormatter(report_layout.pc_date_format))
        axis.tick_params(axis="x", labelsize=report_layout.pc_tick_label_size, pad=4)
        axis.tick_params(axis="y", labelsize=10)
        for tick_label in axis.get_xticklabels():
            tick_label.set_rotation(report_layout.pc_tick_rotation_degrees)
            tick_label.set_horizontalalignment("center")
            tick_label.set_verticalalignment("top")
        axis.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
        axis.legend(
            handles=[
                Line2D([0], [0], color="#8A8A8A", linewidth=1.0, alpha=0.55, label="逐時主成分時間係數 PC"),
                Line2D([0], [0], color="black", linewidth=1.4, label="逐日平均"),
                Line2D([0], [0], color="white", linewidth=0, label="缺測處斷線"),
            ],
            loc="upper right",
            ncol=3,
            frameon=True,
            facecolor="white",
            framealpha=0.92,
        )
        paths = save_report_figure(figure, f"water_column_mode_{mode_number:02d}_pc_report")
        plt.close(figure)
        return paths

    mode_assets: list[dict[str, Any]] = []
    for mode_index in range(config.figure_mode_count):
        mode_number = mode_index + 1
        explained_percent = float(explained_variance[mode_index] * 100.0)
        mode_asset: dict[str, Any] = {
            "mode": mode_number,
            "explained_variance_fraction": float(explained_variance[mode_index]),
            "velocity_levels": [],
        }
        # 每個深度使用自己的速度色階與 q95 比例尺，但所有圖共用同一經緯度範圍；如此既
        # 能看清楚弱流深度，又在 metadata 中保存每張圖的尺度，不把不同深度硬塞成一張圖。
        for level_index, level_id in enumerate(VELOCITY_LEVEL_IDS):
            u_field = regression_u[mode_index, level_index]
            v_field = regression_v[mode_index, level_index]
            magnitude = np.hypot(u_field, v_field)
            color_limit, vector_reference, quiver_scale = velocity_scale(u_field, v_field)
            stem = f"water_column_mode_{mode_number:02d}_{level_id}_spatial_report"
            spatial_paths, transparent_paths = render_vector_map(
                stem,
                magnitude,
                u_field,
                v_field,
                color_limits=(0.0, color_limit),
                vector_reference=vector_reference,
                quiver_scale=quiver_scale,
                # 題名刻意拆成模態／海域與物理層位／解釋變異兩行；主圖與色條已有固定
                # 分欄，雙行仍只佔主地圖上緣，不會再越界遮住右側數值色條。
                title=(
                    f"{plot_name}：奇異值分解 SVD 模態 {mode_number}\n"
                    f"{VELOCITY_LEVEL_LABELS_ZH[level_index]}（解釋變異量：{explained_percent:.2f}%）"
                ),
                colorbar_label="流速振幅（m/s / PC 1σ）",
                vector_unit_label="m/s / PC 1σ",
                valid_mask=velocity_mask[level_index],
            )
            mode_asset["velocity_levels"].append(
                {
                    "level_id": level_id,
                    "level_label_zh": VELOCITY_LEVEL_LABELS_ZH[level_index],
                    "report_files": spatial_paths,
                    "vector_scale_transparent_report_files": transparent_paths,
                    "vector_scale_embedded_in_report_files": True,
                    "velocity_color_limit_mps_per_pc_standard_deviation": color_limit,
                    "vector_reference_mps_per_pc_standard_deviation": vector_reference,
                    "matplotlib_quiver_scale": quiver_scale,
                }
            )

        eta_values = regression_eta[mode_index]
        eta_limit = finite_symmetric_limit(eta_values)
        mode_asset["eta_report_files"] = render_scalar_map(
            f"water_column_mode_{mode_number:02d}_eta_spatial_report",
            eta_values,
            cmap_name="RdBu_r",
            color_limits=(-eta_limit, eta_limit),
            title=(
                f"{plot_name}：奇異值分解 SVD 模態 {mode_number}\n"
                f"自由水面高度 η（解釋變異量：{explained_percent:.2f}%）"
            ),
            colorbar_label="η 回歸幅度（m / PC 1σ）",
        )
        mode_asset["eta_symmetric_color_limit_m_per_pc_standard_deviation"] = eta_limit
        mode_asset["pc_report_files"] = render_pc(mode_number, mode_index, explained_percent)
        mode_assets.append(mode_asset)

    # 解釋變異圖本身也是一張獨立的報告圖，不與任何模態地圖或 PC 共用 axes。
    modes = np.arange(1, explained_variance.size + 1)
    individual_percent = explained_variance * 100.0
    cumulative_percent = np.cumsum(explained_variance) * 100.0
    scree_figure, scree_axis = plt.subplots(figsize=(10.0, 5.3))
    scree_figure.patch.set_facecolor("white")
    bars = scree_axis.bar(
        modes,
        individual_percent,
        width=0.72,
        color="#2A6F97",
        label="單一模態解釋變異量",
        zorder=2,
    )
    scree_axis.plot(
        modes,
        cumulative_percent,
        color="#202020",
        linewidth=1.5,
        marker="o",
        markersize=4.2,
        label="累積解釋變異量",
        zorder=3,
    )
    for index, bar in enumerate(bars):
        scree_axis.text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height() + max(float(np.max(individual_percent)) * 0.015, 0.05),
            f"{individual_percent[index]:.2f}%",
            ha="center",
            va="bottom",
            fontsize=7.6,
            rotation=90 if explained_variance.size > 12 else 0,
        )
    scree_axis.set_title(f"{plot_name}：奇異值分解 SVD 模態解釋變異", fontsize=15, pad=12)
    scree_axis.set_xlabel("模態編號", fontsize=11)
    scree_axis.set_ylabel("解釋變異（%）", fontsize=11)
    scree_axis.set_xlim(0.35, float(modes[-1]) + 0.65)
    scree_axis.set_ylim(0.0, max(100.0, float(np.max(cumulative_percent)) * 1.08))
    scree_axis.set_xticks(modes)
    scree_axis.tick_params(axis="both", labelsize=9)
    scree_axis.grid(axis="y", color="#D9D9D9", linewidth=0.65, alpha=0.9, zorder=1)
    scree_axis.legend(frameon=True, facecolor="white", framealpha=0.92)
    explained_paths = save_report_figure(scree_figure, "water_column_svd_explained_variance_report")
    plt.close(scree_figure)

    # 七張 QC 圖逐張輸出；把一張 2×4 coverage panel 拆開後，報告撰寫者可只引用特定深度
    # 的有效 feature 範圍，也不會把未納入的白色區域誤讀成其他深度的缺測結論。這裡沿用
    # render_scalar_map 的標準色條與固定 0/1 刻度，避免為二值圖另建未被使用的 colormap
    # 而讓圖面契約與實際輸出脫節。
    coverage_assets: list[dict[str, Any]] = []
    for level_index, level_id in enumerate(VELOCITY_LEVEL_IDS):
        coverage_path = render_scalar_map(
            f"water_column_{level_id}_feature_coverage_qc_report",
            velocity_mask[level_index].astype(np.float64),
            cmap_name="Blues",
            color_limits=(0.0, 1.0),
            title=f"{plot_name}：{VELOCITY_LEVEL_LABELS_ZH[level_index]}有效特徵遮罩",
            colorbar_label="有效特徵（1=納入，0=未納入）",
            colorbar_ticks=np.asarray([0.0, 1.0]),
            colorbar_tick_labels=("未納入", "納入"),
        )
        coverage_assets.append(
            {
                "feature_group": level_id,
                "label_zh": VELOCITY_LEVEL_LABELS_ZH[level_index],
                "report_files": coverage_path,
                "cell_count": int(np.count_nonzero(velocity_mask[level_index])),
            }
        )
    eta_coverage_path = render_scalar_map(
        "water_column_eta_feature_coverage_qc_report",
        eta_mask.astype(np.float64),
        cmap_name="Blues",
        color_limits=(0.0, 1.0),
        title=f"{plot_name}：自由水面高度 η 有效特徵遮罩",
        colorbar_label="有效特徵（1=納入，0=未納入）",
        colorbar_ticks=np.asarray([0.0, 1.0]),
        colorbar_tick_labels=("未納入", "納入"),
    )
    coverage_assets.append(
        {
            "feature_group": "eta",
            "label_zh": "自由水面高度 η",
            "report_files": eta_coverage_path,
            "cell_count": int(np.count_nonzero(eta_mask)),
        }
    )

    # 圖檔移出專案後仍需知道每一個 mode 的獨立檔案如何配對；因此把指南與機器可讀
    # manifest 寫入 figures/，並明確記錄文獻只支持「空間與 PC 分開、同 mode 配對」的
    # 科學圖層組織，白底與後製比例尺則是本專案的交付政策。
    report_guide = f"""# 水柱聯合 SVD 圖表報告指南

本 bundle 採 `{WATER_COLUMN_FIGURE_STYLE}`。所有報告圖均為獨立檔案：不再產生把六個
深度、eta 與 PC 放在同一畫布的 `mode_XX.png` 複合圖。

## 每一模態的檔案

- `water_column_mode_XX_<velocity_level>_spatial_report`：一個速度層一張獨立地圖；色彩是
  該層 `sqrt(u²+v²)` 振幅，黑箭頭是同一模態、PC 增加 1 個標準差時的 `u/v` 回歸向量；
  每張正式主圖左下角已內嵌同一 q95 的向量比例尺。
- 同一速度圖的 `_vector_scale_transparent` 是可放入簡報後製的透明比例尺；它與主圖比例尺
  使用相同 q95 物理量，但不應再額外疊加到已含比例尺的主圖。
- `water_column_mode_XX_eta_spatial_report`：唯一自由水面高度 η 的獨立回歸圖，單位為
  `m / PC 1σ`；eta 沒有垂向層，不能與六個速度層重複解讀。
- `water_column_mode_XX_pc_report`：同 mode 的獨立標準化 PC 時序圖。灰線保留逐時變化，
  黑線為只供閱讀的逐日平均；缺測時間段保持斷線。

## 讀圖限制

1. 六層速度、唯一 eta 與共同 PC 是同一次聯合 SVD 的結果；拆成獨立圖檔只改變版面，不是
   重新對各深度各做一次 SVD。
2. 空間圖的回歸幅度表示 PC 增加 1 個樣本標準差時的物理變化；PC 為負時，對應的 eta
   正負與全部速度箭頭方向都反轉。
3. 解釋變異量是聯合加權狀態向量的變異比例，不是某一張速度圖的局部百分比。
4. 暖灰陸地與深灰海岸線僅為地理參照，不會改變分析遮罩、權重、SVD 或有效 feature。

## 建議報告配對

先使用 `water_column_svd_explained_variance_report` 說明模態保留，再以同一 `XX` 配對
任一深度 spatial map、eta map 與 `pc_report`。速度主圖已含比例尺；若報告版面必須將比例尺
移至圖外，才取用同 stem 的 `_vector_scale_transparent.svg`，並移除或遮蔽主圖內嵌比例尺，
不可把兩者同時顯示。完整色階、箭頭尺度、有效格點數與檔案清單均保存於
`plot_metadata.json`。
"""
    report_guide_path = figures_dir / "REPORT_GUIDE.md"
    report_guide_path.write_text(report_guide, encoding="utf-8")
    created.append(str(report_guide_path.relative_to(output_dir)))

    logical_figure_count = int(config.figure_mode_count * (len(VELOCITY_LEVEL_IDS) + 2) + 1 + len(coverage_assets))
    plot_metadata = {
        "schema_name": "ocm_water_column_independent_figure_assets",
        "schema_version": WATER_COLUMN_FIGURE_SCHEMA,
        "style": WATER_COLUMN_FIGURE_STYLE,
        "independent_figure_policy": {
            "enabled": True,
            "description": "每一個深度速度空間圖、唯一 eta 空間圖、每一模態 PC 與 QC 圖皆為單獨 figure；不產生 multi-panel mode 圖。",
            "logical_figure_count": logical_figure_count,
            "mode_count": int(config.figure_mode_count),
            "velocity_maps_per_mode": len(VELOCITY_LEVEL_IDS),
            "eta_maps_per_mode": 1,
            "pc_maps_per_mode": 1,
            "coverage_maps": len(coverage_assets),
        },
        "text_policy": {
            "assets_contain_text": True,
            "scientific_terms": "奇異值分解 SVD、主成分時間係數 PC、m/s、UTC、°E、°N",
        },
        "rendering": {
            "formats": list(config.figure_formats),
            "png_dpi": config.figure_dpi,
            "report_background": "opaque white",
            "report_font_name": report_font_name,
            "report_font_file_sha256": report_font_sha256,
            "analysis_bbox_lon_lat": list(config.domain_bbox),
            "plotted_valid_cell_edge_bbox_lon_lat": [plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max],
            "longitude_axis_ticks_degrees_east": longitude_ticks.tolist(),
            "latitude_axis_ticks_degrees_north": latitude_ticks.tolist(),
            "map_axis_tick_count": report_layout.map_axis_tick_count,
            "quiver_grid_stride": step,
            "vector_scale_policy": "每張速度空間圖主圖右下角以表層圖同款緊湊箭頭、數值與單位內嵌 q95 向量參考尺；另輸出透明比例尺供必要後製",
            "colorbar_boundary_policy": "每張獨立圖的色條首尾等於該圖 vmin/vmax",
        },
        "geographic_context": {
            "land_overlay_source": "OSMData land-polygons derived from OpenStreetMap natural=coastline",
            "logical_path": config.figure_land_overlay_logical_path,
            "sha256": config.figure_land_overlay_sha256,
            "plotted_polygon_count": len(plot_land_polygons),
            "plotted_vertex_count_after_bbox_clipping": coastline_vertex_count,
            "semantics": "visual geographic reference only; does not alter SVD masks, arrays, weights, or statistics",
        },
        "spatial_pattern_representation": {
            "pc": "每一模態以樣本標準差 ddof=1 標準化為無因次 PC。",
            "velocity_scalar": "各速度層 sqrt(u^2+v^2) 回歸振幅，單位 m/s per 1 standard deviation of PC。",
            "velocity_vectors": "各速度層 u/v 回歸向量，單位 m/s per 1 standard deviation of PC。",
            "eta": "唯一自由水面高度 eta 回歸幅度，單位 m per 1 standard deviation of PC。",
            "equivalence": "regression_pattern × standardized_PC 等價於原 physical_loading × raw_PC 的單模態距平重建，僅忽略已記錄的浮點均值尾差。",
        },
        "academic_visual_references": [
            {
                "source": "ResearchGate EOF/PC schematic provided by the user",
                "url": "https://www.researchgate.net/figure/Schematic-of-the-empirical-orthogonal-function-EOF-principal-component-PC-analysis_fig3_372347799",
                "applied_convention": "每個 mode 同時具有空間 EOF/回歸型態與對應 PC 時序；圖面資產分開保存。",
            },
            {
                "source": "Buongiorno Nardelli, Mulet, and Iudicone (2018), JGR Oceans",
                "doi": "10.1002/2017JC013316",
                "url": "https://agupubs.onlinelibrary.wiley.com/doi/10.1002/2017JC013316",
                "applied_convention": "三維海洋變數以深度／垂向結構分開檢視；本專案保留同一聯合 mode 與共同 PC，不把深度拆成不同 SVD。",
            },
            {
                "source": "Hong et al. (2025), Remote Sensing 17(8), 1468",
                "doi": "10.3390/rs17081468",
                "url": "https://www.mdpi.com/2072-4292/17/8/1468",
                "applied_convention": "多變量 EOF/MEOF 的垂向一致性由同一組空間模態與時間係數解讀；本專案以獨立深度圖呈現同一 mode。",
            },
            {
                "source": "Constantinou and Hogg (2021), arXiv:2012.08025",
                "url": "https://arxiv.org/pdf/2012.08025",
                "applied_convention": "EOF/PC 用於海洋變異的空間型態與時間尺度分解；本專案不把 PC 圖與空間圖壓縮在同一 subplot。",
            },
            {
                "source": "Lee et al. (2013), Ocean Dynamics",
                "doi": "10.1007/s10236-013-0643-z",
                "url": "https://link.springer.com/article/10.1007/s10236-013-0643-z",
                "applied_convention": "三維流場的水平型態與垂向剖面分開檢視；本專案將同一聯合 mode 的各深度圖獨立輸出，保留共同 PC 與 eta 的配對關係。",
            },
        ],
        "assets": {
            "modes": mode_assets,
            "explained_variance": {"report_files": explained_paths},
            "coverage": coverage_assets,
        },
    }
    plot_metadata_path = figures_dir / "plot_metadata.json"
    _write_json(plot_metadata_path, plot_metadata)
    created.append(str(plot_metadata_path.relative_to(output_dir)))
    return created, {
        "family": report_font_name,
        "sha256": report_font_sha256,
        "style": WATER_COLUMN_FIGURE_STYLE,
        "schema_version": WATER_COLUMN_FIGURE_SCHEMA,
        "independent_figure_policy": True,
        "logical_figure_count": logical_figure_count,
    }


def _peak_rss_bytes() -> int:
    """回傳目前 process 至今的 peak RSS，統一 macOS 與 Linux 的單位差異。

    Darwin 的 ``ru_maxrss`` 單位是 bytes，Linux 則是 KiB；結果明確轉成 bytes 後寫入
    metadata，讓本機 trial 與 SERVER 正式 run 可以比較，而不會因平台單位不同差 1024 倍。
    """

    raw_value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw_value if sys.platform == "darwin" else raw_value * 1024


def _array_metadata(array: np.ndarray, dimensions: Sequence[str], unit: str) -> dict[str, Any]:
    """建立輸出 NPY 的 shape、dtype、維度與單位描述，供下游直接驗證。"""

    return {
        "shape": [int(value) for value in array.shape],
        "dtype": str(array.dtype),
        "dimensions": list(dimensions),
        "unit": unit,
    }


def _candidate_feature_fraction_maps(
    candidate: CandidateFeatureLayout,
    fractions: np.ndarray,
    grid_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """將 raw feature 有限率展回各深度 u/v 與 eta 規則格，供排除原因檢查。"""

    velocity_fraction = np.full((len(VELOCITY_LEVEL_IDS), 2, *grid_shape), np.nan, dtype=np.float64)
    for level_index in range(len(VELOCITY_LEVEL_IDS)):
        velocity_fraction[level_index, 0, candidate.level_rows[level_index], candidate.level_cols[level_index]] = fractions[candidate.level_u_slices[level_index]]
        velocity_fraction[level_index, 1, candidate.level_rows[level_index], candidate.level_cols[level_index]] = fractions[candidate.level_v_slices[level_index]]
    eta_fraction = np.full(grid_shape, np.nan, dtype=np.float64)
    eta_fraction[candidate.eta_rows, candidate.eta_cols] = fractions[candidate.eta_slice]
    return velocity_fraction, eta_fraction


def _nearest_existing_directory(path: Path) -> Path:
    """找到尚未建立 output root 時可用的父目錄，供非破壞性的磁碟空間預估。"""

    candidate = path.resolve()
    while not candidate.exists():
        if candidate.parent == candidate:
            raise FileNotFoundError(f"找不到可用輸出父目錄: {path}")
        candidate = candidate.parent
    return candidate


def preflight_water_column_multivariate_svd(
    *,
    config_path: Path,
    native_root: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
) -> dict[str, Any]:
    """在正式 native 3D I/O 前驗證後灣或其它完整 domain 的輸入與資源規模。

    預檢不建立任何成果或大型暫存檔。它依完整 flow grid、靜態 bathymetry 與 UTC 時次估計
    raw float32 暫存、最終 float64 加權矩陣及 dense SVD 工作集；實際 feature 數會在讀完
    zcor 後再依有效率縮小。此摘要讓正式 run 在記憶體不足時直接選擇 PROPACK，而不是中止。
    """

    config = load_water_column_config(config_path)
    native_root = native_root.resolve()
    surface_root = surface_root.resolve()
    output_root = output_root.resolve()
    _require(native_root.is_dir(), f"native_root 不存在或不是目錄: {native_root}")
    _require(surface_root.is_dir(), f"surface_root 不存在或不是目錄: {surface_root}")
    grid = _load_full_domain_grid(surface_root, native_root, config)
    timeline = _discover_timeline(
        surface_root,
        native_root,
        config,
        grid,
        allow_partial_months=allow_partial_months,
        allow_trial=allow_trial,
    )
    candidate = _build_candidate_feature_layout(grid, config)
    row_count = int(timeline.time_utc_ns.size)
    feature_count = candidate.feature_count
    raw_bytes = row_count * feature_count * np.dtype(np.float32).itemsize
    weighted_bytes = row_count * feature_count * np.dtype(np.float64).itemsize
    matrix_shape = (feature_count, row_count)
    dense_bytes = _estimated_dense_svd_bytes(matrix_shape)
    storage_anchor = _nearest_existing_directory(output_root)
    free_bytes = int(shutil.disk_usage(storage_anchor).free)
    return {
        "status": "preflight_ok",
        "analysis_kind": WATER_COLUMN_ANALYSIS_KIND,
        "matrix": {
            "symbol": "A",
            "orientation": WATER_COLUMN_MATRIX_ORIENTATION,
            "shape_upper_bound": [feature_count, row_count],
            "storage_order": WATER_COLUMN_MATRIX_ORDER,
        },
        "analysis_label": config.analysis_label,
        "flow_domain_id": config.domain_id,
        "flow_domain_name_zh": config.domain_name_zh,
        "domain_bbox_lon_lat": list(config.domain_bbox),
        "source_flow_domains_config": config.source_flow_domains_config,
        "source_flow_domains_config_sha256": config.source_flow_domains_config_sha256,
        "time_axis": {
            "source_time_count": timeline.source_time_count,
            "canonical_time_count": row_count,
            "first_utc": _iso_utc_from_ns(int(timeline.time_utc_ns[0])),
            "last_utc": _iso_utc_from_ns(int(timeline.time_utc_ns[-1])),
            "median_timestep_hours": timeline.median_timestep_hours,
            "maximum_gap_hours": timeline.maximum_gap_hours,
            "gap_break_count": timeline.gap_break_count,
            "reordered_time_step_count": timeline.reordered_time_step_count,
            "dropped_duplicate_time_step_count": timeline.dropped_duplicate_time_step_count,
        },
        "grid": {
            "shape": [int(value) for value in grid.static_mask.shape],
            "static_ocean_cell_count": int(np.count_nonzero(grid.static_mask & grid.geometry_mask)),
            "selected_native_source_node_count": int(grid.selected_nodes.size),
            "native_source_node_count": grid.source_node_count,
            "selected_native_node_fraction": float(grid.selected_nodes.size / grid.source_node_count),
            "selected_native_node_contiguous_run_count": grid.selected_node_run_count,
            "native_block_read_strategy": config.native_block_read_strategy,
            "candidate_feature_count": feature_count,
        },
        "resource_estimate": {
            "raw_float32_matrix_bytes": raw_bytes,
            "weighted_float64_matrix_bytes_before_dynamic_feature_filter": weighted_bytes,
            "estimated_dense_svd_working_set_bytes": dense_bytes,
            "configured_dense_memory_limit_bytes": config.dense_memory_limit_bytes,
            "planned_solver_if_static_shape_held": "direct_dense_lapack" if dense_bytes <= config.dense_memory_limit_bytes else "direct_propack_streaming",
            "available_output_filesystem_bytes": free_bytes,
            "raw_plus_weighted_bytes_before_dynamic_feature_filter": raw_bytes + weighted_bytes,
        },
    }


def run_water_column_multivariate_svd(
    *,
    config_path: Path,
    native_root: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
    make_figures: bool = True,
    resume_partial_dir: Path | None = None,
) -> Path:
    """執行一個完整 flow domain 的六層聯合 ``u/v/eta`` 直接 SVD。

    成果發布到 ``water_column_svd/<analysis_label_vN>/``，只有所有數值陣列、設定要求的圖表與
    metadata 均完成後才以原子 rename 發布。中間 raw/matrix memory-map 位於同一 `.partial`
    目錄並在發布前移除；它們是運算工作檔，不是科學成果，也不會佔用 immutable run。

    若 ``resume_partial_dir`` 指向先前失敗後保留的 recovery 目錄，本函式只重新驗證小型
    grid／時間 metadata，然後使用其中已完成的加權矩陣重試直接 SVD。這個入口不得跳過
    checkpoint 的設定與 UTC 摘要驗證，因此不能把舊資料或不同 mask 偽裝成同一次結果。
    """

    performance = PerformanceRecorder()
    process_cpu_started = time.process_time()
    baseline_peak_rss = _peak_rss_bytes()
    config = load_water_column_config(config_path)
    native_root = native_root.resolve()
    surface_root = surface_root.resolve()
    output_root = output_root.resolve()
    _require(native_root.is_dir(), f"native_root 不存在或不是目錄: {native_root}")
    _require(surface_root.is_dir(), f"surface_root 不存在或不是目錄: {surface_root}")
    final_dir = output_root / "water_column_svd" / config.analysis_label
    if final_dir.exists():
        raise FileExistsError(f"六層聯合 SVD 成果版本已存在，拒絕覆寫: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    resumed_from_recovery = resume_partial_dir is not None
    if resume_partial_dir is None:
        partial_dir = final_dir.parent / f".{config.analysis_label}.partial-{uuid.uuid4().hex}"
        partial_dir.mkdir(parents=False)
    else:
        # recovery 目錄必須由本成果命名空間持有，避免誤把其他 domain／設定的暫存矩陣原子
        # rename 成後灣正式成果；更細的設定與 UTC 一致性在 checkpoint 載入時再次驗證。
        partial_dir = resume_partial_dir.expanduser().resolve()
        _require(partial_dir.is_dir(), f"resume_partial_dir 不存在或不是目錄: {partial_dir}")
        _require(partial_dir.parent == final_dir.parent, "resume_partial_dir 必須位於同一 water_column_svd 命名空間")
        _require(partial_dir.name.startswith(f".{config.analysis_label}."), "resume_partial_dir 名稱與本次 analysis_label 不符")
    raw_matrix_path = partial_dir / "raw_features_float32.dat"
    weighted_matrix_path = partial_dir / "weighted_anomaly_float64.dat"
    checkpoint_path = partial_dir / SOLVER_RESUME_CHECKPOINT_FILENAME
    failure_diagnostic_path = partial_dir / SOLVER_FAILURE_DIAGNOSTIC_FILENAME
    success = False
    recovery_preserved = False
    try:
        with performance.measure("grid_and_paired_timeline_validation"):
            grid = _load_full_domain_grid(surface_root, native_root, config)
            timeline = _discover_timeline(
                surface_root,
                native_root,
                config,
                grid,
                allow_partial_months=allow_partial_months,
                allow_trial=allow_trial,
            )
            candidate = _build_candidate_feature_layout(grid, config)

        if resumed_from_recovery:
            with performance.measure("solver_resume_checkpoint_validation"):
                resume_state = _load_solver_resume_checkpoint(
                    checkpoint_path,
                    weighted_matrix_path,
                    config=config,
                    timeline=timeline,
                    candidate=candidate,
                )
                selected = resume_state.selected
                retained_time = resume_state.retained_time
                feature_mean = resume_state.feature_mean
                velocity_rms = resume_state.velocity_rms
                eta_rms = resume_state.eta_rms
                feature_scale = resume_state.feature_scale
                total_sum_squares = resume_state.total_sum_squares
        else:
            with performance.measure("paired_native_surface_io_and_raw_feature_matrix"):
                _write_raw_feature_matrix(
                    raw_matrix_path,
                    surface_root=surface_root,
                    native_root=native_root,
                    config=config,
                    grid=grid,
                    timeline=timeline,
                    layout=candidate,
                )

            with performance.measure("dynamic_feature_mask_and_complete_time_selection"):
                selected, complete_time = _select_feature_layout(
                    raw_matrix_path,
                    time_count=timeline.time_utc_ns.size,
                    candidate=candidate,
                    grid=grid,
                    config=config,
                )
                retained_time = timeline.time_utc_ns[complete_time]

            with performance.measure("compact_weighted_matrix_and_normalization"):
                (
                    feature_mean,
                    velocity_rms,
                    eta_rms,
                    feature_scale,
                    total_sum_squares,
                ) = _compact_and_standardize_matrix(
                    raw_matrix_path,
                    weighted_matrix_path,
                    source_time_count=timeline.time_utc_ns.size,
                    selected=selected,
                    complete_time=complete_time,
                    config=config,
                )
                # 原始暫存資料已完成所有可追溯的有效率與平均值計算，立即刪除以釋放 SERVER
                # scratch；正式成果只保存衍生陣列與來源 provenance，不保存大型重複輸入。
                raw_matrix_path.unlink()

            with performance.measure("solver_resume_checkpoint_write"):
                _write_solver_resume_checkpoint(
                    checkpoint_path,
                    config=config,
                    timeline=timeline,
                    candidate=candidate,
                    selected=selected,
                    retained_time=retained_time,
                    feature_mean=feature_mean,
                    velocity_rms=velocity_rms,
                    eta_rms=eta_rms,
                    feature_scale=feature_scale,
                    total_sum_squares=total_sum_squares,
                )

        with performance.measure("direct_svd_solver_and_residual_validation"):
            solution = _solve_direct_svd(
                weighted_matrix_path,
                matrix_shape=(selected.feature_count, retained_time.size),
                config=config,
                total_sum_squares=total_sum_squares,
            )
            # 主持人方向 A=U_A Σ Vh_A；PC 是 ΣVh_A，故直接保留 (mode,time) 形狀。
            # feature-side 的 U_A.T 才是每個 mode 的空間 loading，供 sign 與物理格網回填。
            raw_pc = solution.vh * solution.singular_values[:, None]
            signed_feature_modes, raw_pc, sign_sources = _resolve_mode_signs(
                solution.u.T,
                raw_pc,
                selected,
                grid,
                config,
            )
            # 加權矩陣是 physical anomaly × feature_scale；將主持人方向的 feature loading
            # 除回同一尺度後，
            # ``mode * raw_pc`` 可重建原物理距平。此處沒有把 eta 複製到任何速度深度。
            physical_mode_compact = signed_feature_modes / feature_scale[None, :]
            mode_u, mode_v, mode_eta = _expand_selected_fields(
                physical_mode_compact,
                selected,
                grid.static_mask.shape,
            )
            mean_u, mean_v, mean_eta = _expand_selected_fields(
                feature_mean,
                selected,
                grid.static_mask.shape,
            )
            pc_mean = np.mean(raw_pc, axis=1)
            pc_centered = raw_pc - pc_mean[:, None]
            pc_standard_deviation = np.std(pc_centered, axis=1, ddof=1)
            _require(np.all(np.isfinite(pc_standard_deviation)) and np.all(pc_standard_deviation > 0.0), "設定模態數的 PC 樣本標準差必須均為有限正值")
            pc_standardized = pc_centered / pc_standard_deviation[:, None]
            regression_u = mode_u * pc_standard_deviation[:, None, None, None]
            regression_v = mode_v * pc_standard_deviation[:, None, None, None]
            regression_eta = mode_eta * pc_standard_deviation[:, None, None]
            explained_variance = solution.singular_values**2 / solution.total_sum_squares
            cumulative_explained_variance = np.cumsum(explained_variance)
            velocity_mask, eta_mask = _mask_from_selected_layout(selected, grid.static_mask.shape)
            velocity_feature_valid_fraction, eta_feature_valid_fraction = _candidate_feature_fraction_maps(
                candidate,
                selected.feature_valid_fraction,
                grid.static_mask.shape,
            )

        # checkpoint 與加權矩陣暫時保留至數值陣列、全部水柱獨立圖面資產及 metadata 都完成。若圖面或
        # 序列化意外失敗，finally 可保留相同矩陣讓下次只重試 solver／輸出，而無須再讀兩年
        # native 3D 資料；這段短暫額外磁碟占用優先保障可恢復性。

        with performance.measure("scientific_array_serialization"):
            arrays: dict[str, tuple[np.ndarray, list[str], str]] = {
                "lon.npy": (grid.lon, ["lon"], "degrees_east"),
                "lat.npy": (grid.lat, ["lat"], "degrees_north"),
                "cell_area_m2.npy": (grid.cell_area_m2, ["lat", "lon"], "m2"),
                "bathymetry_m.npy": (grid.bathymetry_m, ["lat", "lon"], "m positive_down"),
                "analysis_geometry_mask.npy": (grid.geometry_mask, ["lat", "lon"], "bool"),
                "static_ocean_mask.npy": (grid.static_mask, ["lat", "lon"], "bool"),
                "velocity_feature_mask.npy": (velocity_mask, ["velocity_level", "lat", "lon"], "bool"),
                "eta_feature_mask.npy": (eta_mask, ["lat", "lon"], "bool"),
                "velocity_candidate_feature_valid_fraction.npy": (velocity_feature_valid_fraction, ["velocity_level", "component", "lat", "lon"], "fraction"),
                "eta_candidate_feature_valid_fraction.npy": (eta_feature_valid_fraction, ["lat", "lon"], "fraction"),
                "time_utc_ns.npy": (retained_time, ["time"], "ns since 1970-01-01T00:00:00Z"),
                "velocity_level_depth_m.npy": (np.asarray(VELOCITY_LEVEL_DEPTHS_M, dtype=np.float64), ["velocity_level"], "m below surface; 0 denotes published surface current"),
                "mean_u_mps.npy": (mean_u, ["velocity_level", "lat", "lon"], "m s-1"),
                "mean_v_mps.npy": (mean_v, ["velocity_level", "lat", "lon"], "m s-1"),
                "mean_eta_m.npy": (mean_eta, ["lat", "lon"], "m"),
                "mode_u_mps_per_raw_pc.npy": (mode_u, ["mode", "velocity_level", "lat", "lon"], "m s-1 per raw PC unit"),
                "mode_v_mps_per_raw_pc.npy": (mode_v, ["mode", "velocity_level", "lat", "lon"], "m s-1 per raw PC unit"),
                "mode_eta_m_per_raw_pc.npy": (mode_eta, ["mode", "lat", "lon"], "m per raw PC unit"),
                "pc.npy": (raw_pc, ["mode", "time"], "weighted standardized raw PC"),
                "pc_standardized.npy": (pc_standardized, ["mode", "time"], "dimensionless; sample standard deviation one"),
                "regression_u_mps_per_pc_std.npy": (regression_u, ["mode", "velocity_level", "lat", "lon"], "m s-1 per one PC standard deviation"),
                "regression_v_mps_per_pc_std.npy": (regression_v, ["mode", "velocity_level", "lat", "lon"], "m s-1 per one PC standard deviation"),
                "regression_eta_m_per_pc_std.npy": (regression_eta, ["mode", "lat", "lon"], "m per one PC standard deviation"),
                "singular_values.npy": (solution.singular_values, ["mode"], "weighted standardized singular value"),
                "explained_variance.npy": (explained_variance, ["mode"], "fraction of total weighted standardized variance"),
                "cumulative_explained_variance.npy": (cumulative_explained_variance, ["mode"], "fraction of total weighted standardized variance"),
            }
            for filename, (array, _dimensions, _unit) in arrays.items():
                np.save(partial_dir / filename, array, allow_pickle=False)
            _write_json(partial_dir / "config.json", config.raw)

        figure_files: list[str] = []
        figure_font: dict[str, str] | None = None
        if make_figures:
            with performance.measure("twenty_mode_and_qc_figure_rendering"):
                figure_files, figure_font = _make_water_column_figures(
                    partial_dir,
                    config=config,
                    grid=grid,
                    time_utc_ns=retained_time,
                    regression_u=regression_u,
                    regression_v=regression_v,
                    regression_eta=regression_eta,
                    pc_standardized=pc_standardized,
                    explained_variance=explained_variance,
                    velocity_mask=velocity_mask,
                    eta_mask=eta_mask,
                )

        # 所有可發布的數值與圖面皆已寫入 partial 後，才移除大型加權矩陣、checkpoint 及先前
        # recovery 的失敗診斷。如此 final directory 不攜帶數十 GiB scratch，也不會把過期
        # traceback 誤當成果 provenance；若此處之前拋出例外，則仍保留可續跑材料。
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        if failure_diagnostic_path.exists():
            failure_diagnostic_path.unlink()
        if weighted_matrix_path.exists():
            weighted_matrix_path.unlink()

        source_metadata = [
            {
                "month_id": descriptor.month_id,
                "cache_kind": descriptor.cache_kind,
                "surface_metadata_sha256": _canonical_json_hash(descriptor.source_metadata),
                "native_metadata_sha256": _canonical_json_hash(descriptor.native_metadata),
            }
            for descriptor in timeline.descriptors
        ]
        science_provenance = {
            "analysis_kind": WATER_COLUMN_ANALYSIS_KIND,
            "matrix_orientation": WATER_COLUMN_MATRIX_ORIENTATION,
            "matrix_order": WATER_COLUMN_MATRIX_ORDER,
            "config": config.raw,
            "source_flow_domains_config_sha256": config.source_flow_domains_config_sha256,
            "source_months": source_metadata,
            "retained_time_count": int(retained_time.size),
            "feature_count": selected.feature_count,
        }
        resource_metadata = {
            "cpu_process_seconds": float(time.process_time() - process_cpu_started),
            "peak_rss_bytes": _peak_rss_bytes(),
            "peak_rss_bytes_at_run_start": baseline_peak_rss,
            "peak_rss_increment_from_run_start_bytes": max(0, _peak_rss_bytes() - baseline_peak_rss),
            "temporary_matrix_storage": {
                "raw_float32_matrix_retained_in_final_output": False,
                "weighted_float64_matrix_retained_in_final_output": False,
                "raw_feature_matrix_bytes_before_removal": int(timeline.time_utc_ns.size * candidate.feature_count * np.dtype(np.float32).itemsize),
                "weighted_matrix_bytes_before_removal": int(retained_time.size * selected.feature_count * np.dtype(np.float64).itemsize),
                "weighted_matrix_orientation": WATER_COLUMN_MATRIX_ORIENTATION,
                "weighted_matrix_storage_order": WATER_COLUMN_MATRIX_ORDER,
            },
            "parallel_execution": {
                "native_io_workers_requested": config.native_io_workers,
                "native_io_workers_effective": min(config.native_io_workers, os.cpu_count() or 1),
                "native_block_read_strategy": config.native_block_read_strategy,
                "native_time_block_size": config.native_time_block_size,
                "linear_algebra_threads_requested": config.linear_algebra_threads,
                "linear_algebra_threads_effective": min(config.linear_algebra_threads, os.cpu_count() or 1),
                "policy": "paired native/surface time blocks parallel read; raw feature memory-map is written only by the main thread; direct SVD starts after I/O completes",
            },
            "native_block_read_strategy": {
                "strategy": config.native_block_read_strategy,
                "selected_node_count": int(grid.selected_nodes.size),
                "source_node_count": grid.source_node_count,
                "selected_node_fraction": float(grid.selected_nodes.size / grid.source_node_count),
                "selected_node_contiguous_run_count": grid.selected_node_run_count,
                "reason": "explicit configuration controls only native I/O materialization; selected physical values, interpolation, masks, and SVD features are unchanged",
            },
        }
        metadata = {
            "schema_name": "ocm_water_column_multivariate_svd",
            "schema_version": "1.0.0",
            "status": "trial_pilot" if any(descriptor.cache_kind == "trial_partial_month" for descriptor in timeline.descriptors) else "flow_domain_pilot_complete",
            "analysis_kind": WATER_COLUMN_ANALYSIS_KIND,
            "analysis_label": config.analysis_label,
            "purpose": config.raw.get("purpose"),
            "science_provenance_sha256": _canonical_json_hash(science_provenance),
            "domain": {
                "flow_domain_id": config.domain_id,
                "name_zh": config.domain_name_zh,
                "bbox_lon_lat": list(config.domain_bbox),
                "center_lonlat": list(config.domain_center),
                "source_flow_domains_config": config.source_flow_domains_config,
                "source_flow_domains_config_sha256": config.source_flow_domains_config_sha256,
            },
            "vertical_sampling": {
                "velocity_level_ids": list(VELOCITY_LEVEL_IDS),
                "velocity_level_labels_zh": list(VELOCITY_LEVEL_LABELS_ZH),
                "velocity_level_depth_m": list(VELOCITY_LEVEL_DEPTHS_M),
                "surface_velocity_source": "published ocm_surface u/v; highest finite native zcor layer selected upstream",
                "subsurface_velocity_source": "paired ocm_native hvel/zcor; linear interpolation between finite bracketing layers only; no extrapolation",
                "eta_source": "paired ocm_surface eta_m; one 2D free-surface field only, not repeated by depth",
                "vertical_quadrature_weights_m": list(config.vertical_weights_m),
            },
            "input": {
                "years": list(config.years),
                "months": list(config.months),
                "source_months": source_metadata,
                "time_axis_canonicalization": {
                    "policy": config.time_axis_policy,
                    "source_time_count": timeline.source_time_count,
                    "retained_time_count_before_missing_data": int(timeline.time_utc_ns.size),
                    "retained_time_count_after_missing_data": int(retained_time.size),
                    "reordered_time_step_count": timeline.reordered_time_step_count,
                    "dropped_duplicate_time_step_count": timeline.dropped_duplicate_time_step_count,
                    "median_timestep_hours": timeline.median_timestep_hours,
                    "maximum_gap_hours": timeline.maximum_gap_hours,
                    "gap_break_count": timeline.gap_break_count,
                    "first_utc": _iso_utc_from_ns(int(retained_time[0])),
                    "last_utc": _iso_utc_from_ns(int(retained_time[-1])),
                },
            },
            "mask_and_missing_data": {
                "policy": "depth_specific_feature_masks_plus_common_complete_time_rows_no_imputation",
                "feature_selection_policy": selected.feature_selection_policy,
                "minimum_feature_valid_fraction": config.minimum_feature_valid_fraction,
                "minimum_retained_time_fraction": config.minimum_retained_time_fraction,
                "retained_time_fraction": float(retained_time.size / timeline.time_utc_ns.size),
                "velocity_feature_cell_counts": {
                    VELOCITY_LEVEL_IDS[index]: int(selected.level_rows[index].size)
                    for index in range(len(VELOCITY_LEVEL_IDS))
                },
                "eta_feature_cell_count": int(selected.eta_rows.size),
                "missing_value_policy": "NaN features/rows are excluded; never filled with zero or interpolated",
            },
            "svd": {
                "matrix_symbol": "A",
                "matrix_orientation": WATER_COLUMN_MATRIX_ORIENTATION,
                "matrix_storage_order": WATER_COLUMN_MATRIX_ORDER,
                "state_vector_order": "[eta_once,all_surface_to_50m_u,all_surface_to_50m_v] with depth-specific valid cells",
                "matrix_shape": [int(selected.feature_count), int(retained_time.size)],
                "rows": "feature; eta once, then all u depths, then all v depths",
                "columns": "retained UTC time samples",
                "requested_mode_count": config.requested_mode_count,
                "mode_count": int(solution.singular_values.size),
                "normalization": "u/v share volume-weighted RMS; eta uses area-weighted RMS",
                "velocity_rms_mps": velocity_rms,
                "eta_rms_m": eta_rms,
                "spatial_weight": "sqrt(cell_area_m2 * vertical_quadrature_weight_m) for each u/v feature; sqrt(cell_area_m2) for eta",
                "sign_convention": "domain-center surface u, then v, then eta, then largest absolute loading positive",
                "sign_sources": list(sign_sources),
                "retained_mode_relative_reconstruction_error": solution.retained_reconstruction_error,
                "left_relative_residuals": solution.left_residuals.tolist(),
                "right_relative_residuals": solution.right_residuals.tolist(),
                "orthogonality_max_abs_error": solution.orthogonality_max_abs_error,
                "solver": solution.solver_metadata,
            },
            "arrays": {
                filename: _array_metadata(array, dimensions, unit)
                for filename, (array, dimensions, unit) in arrays.items()
            },
            "figures": {
                "enabled": make_figures,
                "requested_mode_count": config.figure_mode_count,
                # 這個欄位只代表 mode index 的數量，不能解讀為實際圖檔數；每個 mode
                # 會再拆成六張速度圖、一張 eta 圖與一張 PC 圖，速度圖另有衍生比例尺資產。
                "mode_figure_count": config.figure_mode_count if make_figures else 0,
                "mode_figure_count_semantics": "mode index count only; no composite mode figure is generated",
                "composite_mode_figure_count": 0,
                "independent_figure_count": (
                    config.figure_mode_count * (len(VELOCITY_LEVEL_IDS) + 2) + 1 + len(VELOCITY_LEVEL_IDS) + 1
                    if make_figures
                    else 0
                ),
                "files": figure_files,
                "font": figure_font,
                "style": WATER_COLUMN_FIGURE_STYLE,
                "schema_version": WATER_COLUMN_FIGURE_SCHEMA,
                "independent_figure_policy": True,
                "description": "每個聯合 mode 的六層速度空間圖、唯一 eta 空間圖與共同 PC 時序圖均各自獨立輸出；解釋變異圖與七張 feature coverage QC 圖亦各自獨立。",
            },
            "performance": performance.to_metadata(
                scope_end="從 runner 入口至 metadata 組裝前；不含 metadata.json 寫入與原子 rename。"
            ),
            "resources": resource_metadata,
            "limitations": [
                "此成果是表層與固定水下 10–50 m 六層聯合 SVD，不宣稱每個格點均代表完整水柱。",
                "各深度 feature 遮罩可因 bathymetry、zcor 包夾與缺值而不同；所有保留 feature 仍在同一 SVD 與同一組 PC 中求解。",
                "eta 為唯一自由水面高度欄位，不具垂向層且未重複進入狀態向量。",
            ],
        }
        _write_json(partial_dir / "metadata.json", metadata)
        os.replace(partial_dir, final_dir)
        success = True
        return final_dir
    except BaseException as error:
        # 若加權矩陣與 checkpoint 均已完成，失敗不應再刪掉五小時建立的直接 SVD 輸入。保留的
        # recovery 目錄不在正式 final namespace，且必須透過 --resume-partial 的 hash／UTC 驗證
        # 才能使用；若失敗早於 checkpoint，仍沿用原本清除未完成 raw scratch 的策略。
        if checkpoint_path.is_file() and weighted_matrix_path.is_file():
            try:
                _write_solver_failure_diagnostic(
                    failure_diagnostic_path,
                    error=error,
                    checkpoint_path=checkpoint_path,
                    weighted_matrix_path=weighted_matrix_path,
                    resumed_from_recovery=resumed_from_recovery,
                )
                recovery_dir = final_dir.parent / f".{config.analysis_label}.recovery-{uuid.uuid4().hex}"
                os.replace(partial_dir, recovery_dir)
                partial_dir = recovery_dir
                recovery_preserved = True
                print(f"可續跑的直接 SVD checkpoint 已保留於: {recovery_dir}", file=sys.stderr)
            except Exception as preservation_error:
                # checkpoint 保留作業若本身也失敗，不能掩蓋原始科學計算 exception；stderr 中的
                # 次要訊息可讓 SERVER 管理者釐清磁碟／權限問題，finally 則負責既有安全清理。
                print(f"無法保留 SVD recovery checkpoint: {preservation_error}", file=sys.stderr)
        raise
    finally:
        # 成功時 partial 已原子改名；失敗時刪除未發布的大型暫存檔，避免下次正式 run 因
        # 殘留數十 GiB scratch 而誤判磁碟不足。唯一例外是已有可驗證 checkpoint 的 recovery
        # 目錄：它保存相同加權矩陣，供直接 SVD 續跑；任何已發布 final_dir 都不會在此處觸碰。
        if not success and not recovery_preserved and partial_dir.exists():
            shutil.rmtree(partial_dir)
