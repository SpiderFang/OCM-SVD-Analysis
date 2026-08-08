"""將既有水柱直接 SVD 轉成主持人指定的 feature×time 聯合矩陣表示。

主持人示意圖把單一 UTC 時次的狀態向量直式排列為 ``eta``、全部深度的 ``u``、再全部
深度的 ``v``；將每個時次並排後得到 ``A``，其 shape 為 ``(feature, time)``。既有正式
run 以較適合串流 I/O 的 ``X=(time, feature)`` 求直接 SVD，且 eta 位於特徵軸最後。兩者
僅差一個轉置與固定置換：``A = P @ X.T``。若 ``X = U @ Sigma @ Vh``，則
``A = (P @ V) @ Sigma @ U.T``，因此不需要重讀兩年 native 資料或重新進行數值求解，仍可
得到同一個直接 top-20 SVD 的精確重表示。

本模組將這個等價關係落實為可稽核的 immutable 衍生成果：保存主持人順序的左奇異向量、
時間右奇異向量、每一個 feature 的 grid/layer 對照表、物理單位回填尺度，以及由 compact
空間向量回填六層圖場的 round-trip 驗證。它不會改寫來源 run，也不會以 rank-20 重建值取代
原始科學矩陣。
"""

from __future__ import annotations

import csv
import hashlib
import math
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .surface_multivariate_svd import _canonical_json_hash, _read_json_object, _require, _write_json
from .water_column_multivariate_svd import (
    VELOCITY_LEVEL_DEPTHS_M,
    VELOCITY_LEVEL_IDS,
    WATER_COLUMN_ANALYSIS_KIND,
    WaterColumnConfig,
    load_water_column_config,
)


HOST_LAYOUT_SCHEMA_VERSION = "1.0.0"
"""主持人 feature×time 轉置表示的 metadata schema 版本。"""

HOST_LAYOUT_ID = "eta_u_all_depths_v_all_depths_feature_by_time_v1"
"""固定的主持人狀態向量順序與矩陣方向版本。

列順序必須是一次 eta、依表層至 50 m 排列的全部 u、再依相同深度及格點順序排列的全部 v；
欄為 UTC 時次。變更這個順序會改變 feature index map，因此必須升版，不能覆寫既有成果。
"""

HOST_LAYOUT_REQUIRED_SOURCE_FILENAMES = (
    "metadata.json",
    "config.json",
    "lon.npy",
    "lat.npy",
    "cell_area_m2.npy",
    "time_utc_ns.npy",
    "velocity_feature_mask.npy",
    "eta_feature_mask.npy",
    "mean_u_mps.npy",
    "mean_v_mps.npy",
    "mean_eta_m.npy",
    "mode_u_mps_per_raw_pc.npy",
    "mode_v_mps_per_raw_pc.npy",
    "mode_eta_m_per_raw_pc.npy",
    "pc.npy",
    "singular_values.npy",
)
"""建立主持人版向量所需的最小已發布來源檔案。

清單刻意不含 native/surface 快取、raw matrix 或 PROPACK checkpoint。來源 run 已保存將空間
右奇異向量去除尺度後的物理模態、均值、PC、奇異值與遮罩，足以精確轉回 ``A=P@X.T`` 的
設定模態數的因子，同時保證衍生流程不會讀取或重建兩年輸入資料。
"""


@dataclass(frozen=True)
class HostLayoutSource:
    """唯讀來源 run 中重建主持人版因子所需的陣列與科學 metadata。

    ``mode_u/v/eta`` 是已由原始 runner 解除物理尺度後、每 raw PC 單位的空間模態；本模組
    依 cell area、垂向權重及 group RMS 乘回尺度，才能得到主持人矩陣 ``A`` 的加權左奇異
    向量。所有 numpy 陣列以 memory-map 開啟，避免為輸出 feature index map 額外複製大型
    科學陣列到 RAM。
    """

    run_dir: Path
    metadata: dict[str, Any]
    config: WaterColumnConfig
    lon: np.ndarray
    lat: np.ndarray
    cell_area_m2: np.ndarray
    time_utc_ns: np.ndarray
    velocity_mask: np.ndarray
    eta_mask: np.ndarray
    mean_u: np.ndarray
    mean_v: np.ndarray
    mean_eta: np.ndarray
    mode_u: np.ndarray
    mode_v: np.ndarray
    mode_eta: np.ndarray
    pc: np.ndarray
    singular_values: np.ndarray


@dataclass(frozen=True)
class HostLayoutFactors:
    """主持人版 ``A=(feature,time)`` 的設定模態數 SVD 因子與回填對照資料。

    ``left_singular_vectors_weighted`` 即 ``A`` 的左奇異向量，其列順序嚴格對應
    ``feature_index_map.csv``；``right_singular_vectors_time`` 是 ``A`` 的 ``Vh``。以
    ``left @ diag(singular_values) @ right`` 可得到設定 rank 的加權、中心化矩陣近似；若要
    回到物理單位，必須先除以 ``feature_scale``、再加上 ``feature_mean_physical``。
    """

    left_singular_vectors_weighted: np.ndarray
    right_singular_vectors_time: np.ndarray
    singular_values: np.ndarray
    pc: np.ndarray
    feature_scale: np.ndarray
    feature_mean_physical: np.ndarray
    feature_component: np.ndarray
    feature_level_index: np.ndarray
    feature_row: np.ndarray
    feature_col: np.ndarray

    @property
    def feature_count(self) -> int:
        """回傳主持人矩陣的列數，也就是完整狀態向量長度。"""

        return int(self.feature_scale.size)


def _sha256_file(path: Path) -> str:
    """以固定區塊計算檔案 SHA-256，避免讀入大型 NPY 造成不必要 RAM 使用。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _open_array(run_dir: Path, filename: str) -> np.ndarray:
    """以唯讀 memory-map 開啟已發布 NPY，拒絕缺檔與 pickle 內容。"""

    path = run_dir / filename
    _require(path.is_file(), f"主持人版矩陣來源缺少必要陣列: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _load_host_layout_source(run_dir: Path) -> HostLayoutSource:
    """驗證既有水柱 run 並讀入主持人版向量所需的唯讀資料。

    此檢查不只是檔案存在性：它確認模式數、格網、遮罩、PC 與來源 matrix shape 一致，防止
    將不相干的圖面、不同時段 PC 或 eta 重複欄位混入 feature×time 表示。來源 run 必須是
    已發布的完整水柱直接 SVD，而非未完成 scratch 或 figure bundle。
    """

    resolved = run_dir.resolve()
    _require(resolved.is_dir(), f"水柱 SVD 來源 run 不存在: {resolved}")
    metadata = _read_json_object(resolved / "metadata.json")
    _require(
        metadata.get("schema_name") == "ocm_water_column_multivariate_svd"
        and metadata.get("analysis_kind") == WATER_COLUMN_ANALYSIS_KIND,
        "主持人版矩陣來源必須是已發布的 water_column_multivariate_svd run",
    )
    _require(metadata.get("analysis_label") == resolved.name, "來源 metadata.analysis_label 必須與目錄名稱一致")
    config = load_water_column_config(resolved / "config.json")
    _require(config.analysis_label == resolved.name, "來源 config.analysis_label 必須與目錄名稱一致")

    lon = _open_array(resolved, "lon.npy")
    lat = _open_array(resolved, "lat.npy")
    cell_area_m2 = _open_array(resolved, "cell_area_m2.npy")
    time_utc_ns = _open_array(resolved, "time_utc_ns.npy")
    velocity_mask = _open_array(resolved, "velocity_feature_mask.npy")
    eta_mask = _open_array(resolved, "eta_feature_mask.npy")
    mean_u = _open_array(resolved, "mean_u_mps.npy")
    mean_v = _open_array(resolved, "mean_v_mps.npy")
    mean_eta = _open_array(resolved, "mean_eta_m.npy")
    mode_u = _open_array(resolved, "mode_u_mps_per_raw_pc.npy")
    mode_v = _open_array(resolved, "mode_v_mps_per_raw_pc.npy")
    mode_eta = _open_array(resolved, "mode_eta_m_per_raw_pc.npy")
    pc = _open_array(resolved, "pc.npy")
    singular_values = _open_array(resolved, "singular_values.npy")

    _require(lon.ndim == 1 and lon.size >= 2 and np.all(np.isfinite(lon)), "來源 lon.npy 必須是有限一維座標")
    _require(lat.ndim == 1 and lat.size >= 2 and np.all(np.isfinite(lat)), "來源 lat.npy 必須是有限一維座標")
    _require(np.all(np.diff(lon) > 0.0) and np.all(np.diff(lat) > 0.0), "來源 lon/lat 必須嚴格遞增")
    grid_shape = (lat.size, lon.size)
    mode_count = int(singular_values.size)
    _require(
        mode_count == config.requested_mode_count,
        "主持人版來源的實際模態數必須等於來源設定要求的模態數",
    )
    _require(singular_values.ndim == 1 and np.all(np.isfinite(singular_values)) and np.all(singular_values > 0.0), "來源奇異值必須是有限正值")
    _require(
        time_utc_ns.ndim == 1
        and time_utc_ns.dtype == np.int64
        and time_utc_ns.size >= mode_count,
        "來源 UTC 軸不合法或時間數少於來源模態數",
    )
    _require(np.all(np.diff(time_utc_ns) > 0), "來源 UTC 軸必須嚴格遞增")
    expected_velocity_shape = (mode_count, len(VELOCITY_LEVEL_IDS), *grid_shape)
    _require(mode_u.shape == expected_velocity_shape and mode_v.shape == expected_velocity_shape, "來源 u/v mode shape 與六層規則格不一致")
    _require(mode_eta.shape == (mode_count, *grid_shape), "來源 eta mode 必須是 (mode,lat,lon)，不得有 depth 軸")
    _require(mean_u.shape == expected_velocity_shape[1:] and mean_v.shape == expected_velocity_shape[1:], "來源 u/v mean shape 不一致")
    _require(mean_eta.shape == grid_shape, "來源 eta mean shape 不一致")
    _require(cell_area_m2.shape == grid_shape and np.all(np.isfinite(cell_area_m2)) and np.all(cell_area_m2 > 0.0), "來源 cell_area_m2 必須是有限正值格網")
    _require(velocity_mask.shape == expected_velocity_shape[1:] and velocity_mask.dtype == bool, "來源 velocity feature mask shape 不一致")
    _require(eta_mask.shape == grid_shape and eta_mask.dtype == bool, "來源 eta feature mask shape 不一致")
    _require(pc.shape == (mode_count, time_utc_ns.size) and np.all(np.isfinite(pc)), "來源 PC 必須是 (mode,time) 且與 UTC 軸一致")
    _require(np.all(np.isfinite(mode_u[:, velocity_mask])) and np.all(np.isfinite(mode_v[:, velocity_mask])), "有效速度模式值不可含 NaN")
    _require(np.all(np.isfinite(mode_eta[:, eta_mask])), "有效 eta 模式值不可含 NaN")

    source_svd = metadata.get("svd")
    _require(isinstance(source_svd, dict), "來源 metadata 缺少 svd 區段")
    matrix_shape = source_svd.get("matrix_shape")
    _require(isinstance(matrix_shape, list) and len(matrix_shape) == 2, "來源 metadata.svd.matrix_shape 不合法")
    matrix_orientation = str(source_svd.get("matrix_orientation", "time_by_feature"))
    if matrix_orientation == "feature_by_time":
        _require(int(matrix_shape[1]) == time_utc_ns.size, "來源矩陣的時間欄軸與 time_utc_ns 不一致")
    else:
        _require(int(matrix_shape[0]) == time_utc_ns.size, "來源矩陣的時間列軸與 time_utc_ns 不一致")
    return HostLayoutSource(
        run_dir=resolved,
        metadata=metadata,
        config=config,
        lon=lon,
        lat=lat,
        cell_area_m2=cell_area_m2,
        time_utc_ns=time_utc_ns,
        velocity_mask=np.asarray(velocity_mask, dtype=bool),
        eta_mask=np.asarray(eta_mask, dtype=bool),
        mean_u=mean_u,
        mean_v=mean_v,
        mean_eta=mean_eta,
        mode_u=mode_u,
        mode_v=mode_v,
        mode_eta=mode_eta,
        pc=pc,
        singular_values=np.asarray(singular_values, dtype=np.float64),
    )


def _append_feature_block(
    *,
    left_parts: list[np.ndarray],
    scale_parts: list[np.ndarray],
    mean_parts: list[np.ndarray],
    component_parts: list[np.ndarray],
    level_parts: list[np.ndarray],
    row_parts: list[np.ndarray],
    col_parts: list[np.ndarray],
    physical_modes: np.ndarray,
    physical_mean: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    feature_scale: np.ndarray,
    component_code: int,
    level_index: int,
) -> None:
    """將一個 eta、u 或 v feature 區塊依主持人順序加入 compact 左奇異向量。

    ``physical_modes`` 是來源已發布的每 raw PC 單位物理模態，shape 為
    ``(mode,lat,lon)``。SVD 本身使用加權且標準化的欄位，故須乘回 ``feature_scale`` 才能
    成為主持人矩陣 ``A=P@X.T`` 的左奇異向量元素。rows/cols 一律來自最終 feature mask，
    因此陸地、海床以下或缺值排除的位置不會佔用 compact matrix 的假欄位。
    """

    _require(rows.ndim == cols.ndim == 1 and rows.size == cols.size, "feature block rows/cols 必須是一對一")
    _require(feature_scale.shape == (rows.size,), "feature block scale 長度不一致")
    if rows.size == 0:
        return
    values = np.asarray(physical_modes[:, rows, cols], dtype=np.float64).T
    _require(values.shape == (rows.size, physical_modes.shape[0]), "抽取出的 physical mode block shape 不一致")
    _require(np.all(np.isfinite(values)), "有效 feature 的 physical mode 不可含 NaN")
    left_parts.append(values * feature_scale[:, None])
    scale_parts.append(feature_scale)
    mean_parts.append(np.asarray(physical_mean[rows, cols], dtype=np.float64))
    component_parts.append(np.full(rows.size, component_code, dtype=np.int8))
    level_parts.append(np.full(rows.size, level_index, dtype=np.int8))
    row_parts.append(np.asarray(rows, dtype=np.int32))
    col_parts.append(np.asarray(cols, dtype=np.int32))


def _build_host_layout_factors(source: HostLayoutSource) -> HostLayoutFactors:
    """建立 ``A=(feature,time)`` 的 eta→u-all-depths→v-all-depths SVD 因子。

    特徵排列嚴格對應主持人圖：先放 surface eta 格點，再依 surface、10、20、30、40、50 m
    放入所有有效 u 格點，最後用相同深度與格點順序放入 v。這使玩具圖中的 ``u1...uN`` 與
    ``v1...vN`` 能直接透過 feature index map 反查 layer/row/col。
    """

    source_svd = source.metadata["svd"]
    velocity_rms = float(source_svd["velocity_rms_mps"])
    eta_rms = float(source_svd["eta_rms_m"])
    _require(math.isfinite(velocity_rms) and velocity_rms > 0.0, "來源 velocity RMS 不合法")
    _require(math.isfinite(eta_rms) and eta_rms > 0.0, "來源 eta RMS 不合法")

    left_parts: list[np.ndarray] = []
    scale_parts: list[np.ndarray] = []
    mean_parts: list[np.ndarray] = []
    component_parts: list[np.ndarray] = []
    level_parts: list[np.ndarray] = []
    row_parts: list[np.ndarray] = []
    col_parts: list[np.ndarray] = []

    # 主持人指定 eta 只出現一次且位於向量最前；eta 不帶速度 depth index，因此以 -1 記錄。
    eta_rows, eta_cols = np.nonzero(source.eta_mask)
    eta_scale = np.sqrt(np.asarray(source.cell_area_m2[eta_rows, eta_cols], dtype=np.float64)) / eta_rms
    _append_feature_block(
        left_parts=left_parts,
        scale_parts=scale_parts,
        mean_parts=mean_parts,
        component_parts=component_parts,
        level_parts=level_parts,
        row_parts=row_parts,
        col_parts=col_parts,
        physical_modes=source.mode_eta,
        physical_mean=source.mean_eta,
        rows=eta_rows,
        cols=eta_cols,
        feature_scale=eta_scale,
        component_code=0,
        level_index=-1,
    )

    # 圖中的 u1...uN 依六個速度層順序連續編號；每層只加入其 own final mask 的格點，避免
    # 將深水不存在的速度偽裝成 0 或要求 eta 隨深度重複。
    for level_index, vertical_weight_m in enumerate(source.config.vertical_weights_m):
        rows, cols = np.nonzero(source.velocity_mask[level_index])
        velocity_scale = (
            np.sqrt(np.asarray(source.cell_area_m2[rows, cols], dtype=np.float64) * vertical_weight_m)
            / velocity_rms
        )
        _append_feature_block(
            left_parts=left_parts,
            scale_parts=scale_parts,
            mean_parts=mean_parts,
            component_parts=component_parts,
            level_parts=level_parts,
            row_parts=row_parts,
            col_parts=col_parts,
            physical_modes=source.mode_u[:, level_index],
            physical_mean=source.mean_u[level_index],
            rows=rows,
            cols=cols,
            feature_scale=velocity_scale,
            component_code=1,
            level_index=level_index,
        )

    # v block 與 u block 使用完全相同的 layer/mask 遍歷順序，讓同一個水平與垂向位置的
    # u_i/v_i 能由 feature index map 直接配對；這比以不同缺值規則各自編號更可稽核。
    for level_index, vertical_weight_m in enumerate(source.config.vertical_weights_m):
        rows, cols = np.nonzero(source.velocity_mask[level_index])
        velocity_scale = (
            np.sqrt(np.asarray(source.cell_area_m2[rows, cols], dtype=np.float64) * vertical_weight_m)
            / velocity_rms
        )
        _append_feature_block(
            left_parts=left_parts,
            scale_parts=scale_parts,
            mean_parts=mean_parts,
            component_parts=component_parts,
            level_parts=level_parts,
            row_parts=row_parts,
            col_parts=col_parts,
            physical_modes=source.mode_v[:, level_index],
            physical_mean=source.mean_v[level_index],
            rows=rows,
            cols=cols,
            feature_scale=velocity_scale,
            component_code=2,
            level_index=level_index,
        )

    _require(left_parts and scale_parts and mean_parts, "主持人版向量不可為空")
    left = np.concatenate(left_parts, axis=0)
    feature_scale = np.concatenate(scale_parts)
    feature_mean = np.concatenate(mean_parts)
    component = np.concatenate(component_parts)
    level = np.concatenate(level_parts)
    rows = np.concatenate(row_parts)
    cols = np.concatenate(col_parts)
    _require(np.all(np.isfinite(left)) and np.all(np.isfinite(feature_scale)) and np.all(feature_scale > 0.0), "主持人版左奇異向量或尺度不合法")
    _require(np.all(np.isfinite(feature_mean)), "主持人版 physical mean 不可含 NaN")

    source_matrix_shape = source_svd["matrix_shape"]
    source_orientation = str(source_svd.get("matrix_orientation", "time_by_feature"))
    source_feature_count = int(source_matrix_shape[0] if source_orientation == "feature_by_time" else source_matrix_shape[1])
    _require(left.shape[0] == source_feature_count, "主持人版 feature 數必須等於來源 SVD 矩陣的 feature 數")
    _require(left.shape[1] == source.singular_values.size, "主持人版左奇異向量 mode 數不一致")

    # 來源 X 的 raw PC 定義為 Sigma @ U_X.T。因 A=P@X.T，A 的 Vh 正是 U_X.T；直接以
    # PC / singular value 取得可保持來源符號約定的時間右奇異向量，沒有重新估計或旋轉模態。
    right = np.asarray(source.pc, dtype=np.float64) / source.singular_values[:, None]
    _require(np.all(np.isfinite(right)), "主持人版時間右奇異向量不可含 NaN")
    return HostLayoutFactors(
        left_singular_vectors_weighted=left,
        right_singular_vectors_time=right,
        singular_values=source.singular_values.copy(),
        pc=np.asarray(source.pc, dtype=np.float64).copy(),
        feature_scale=feature_scale,
        feature_mean_physical=feature_mean,
        feature_component=component,
        feature_level_index=level,
        feature_row=rows,
        feature_col=cols,
    )


def reconstruct_host_layout_physical_modes(
    source: HostLayoutSource,
    factors: HostLayoutFactors,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """由主持人版 compact 左奇異向量解除尺度並散回六層 u/v 與一次 eta 格網。

    這是主持人圖右側矩陣回推左側圖的實作核心。``left_singular_vectors_weighted`` 的每一列
    對應 ``feature_index_map.csv`` 的一列；先除以該 feature 的面積、垂向權重與 RMS 尺度，
    再依 component、layer、row、col 放回規則格網。回傳的 u/v shape 是
    ``(mode, six_velocity_level, lat, lon)``，eta shape 則是 ``(mode, lat, lon)``；所有未
    納入矩陣的陸地、海床以下或缺值位置都保留 ``NaN``，以維持缺值的物理意義。
    """

    mode_count = factors.singular_values.size
    grid_shape = (source.lat.size, source.lon.size)
    restored_u = np.full((mode_count, len(VELOCITY_LEVEL_IDS), *grid_shape), np.nan, dtype=np.float64)
    restored_v = np.full_like(restored_u, np.nan)
    restored_eta = np.full((mode_count, *grid_shape), np.nan, dtype=np.float64)
    physical_left = factors.left_singular_vectors_weighted / factors.feature_scale[:, None]

    for index in range(factors.feature_count):
        component = int(factors.feature_component[index])
        level = int(factors.feature_level_index[index])
        row = int(factors.feature_row[index])
        col = int(factors.feature_col[index])
        values = physical_left[index]
        if component == 0:
            restored_eta[:, row, col] = values
        elif component == 1:
            restored_u[:, level, row, col] = values
        elif component == 2:
            restored_v[:, level, row, col] = values
        else:  # pragma: no cover - component code 由本模組固定產生，保留防護避免 silent map corruption。
            raise ValueError(f"不支援的主持人 feature component code: {component}")

    return restored_u, restored_v, restored_eta


def _roundtrip_validation(
    source: HostLayoutSource,
    factors: HostLayoutFactors,
    *,
    restored_u: np.ndarray,
    restored_v: np.ndarray,
    restored_eta: np.ndarray,
) -> dict[str, float]:
    """驗證由主持人版左／右奇異向量回填的圖場及 PC 與來源成果相同。

    呼叫者必須傳入 ``reconstruct_host_layout_physical_modes`` 的結果，讓待發布的
    ``roundtrip_mode_*.npy`` 與驗證數值使用完全同一份陣列，避免「驗證的是一份資料、交付
    的是另一份資料」。除了空間模態，也以 ``Sigma @ Vh_A`` 恢復 PC，確認右側時間因子能
    正確配對到左側圖場。
    """

    mode_count = factors.singular_values.size
    grid_shape = (source.lat.size, source.lon.size)
    _require(
        restored_u.shape == (mode_count, len(VELOCITY_LEVEL_IDS), *grid_shape)
        and restored_v.shape == restored_u.shape
        and restored_eta.shape == (mode_count, *grid_shape),
        "round-trip 圖場 shape 與六層規則格網不一致",
    )
    velocity_mask = source.velocity_mask
    eta_mask = source.eta_mask
    u_difference = np.abs(restored_u[:, velocity_mask] - np.asarray(source.mode_u)[:, velocity_mask])
    v_difference = np.abs(restored_v[:, velocity_mask] - np.asarray(source.mode_v)[:, velocity_mask])
    eta_difference = np.abs(restored_eta[:, eta_mask] - np.asarray(source.mode_eta)[:, eta_mask])
    _require(
        np.all(np.isnan(restored_u[:, ~velocity_mask])) and np.all(np.isnan(restored_v[:, ~velocity_mask])),
        "round-trip 後無效速度格點必須維持 NaN",
    )
    _require(np.all(np.isnan(restored_eta[:, ~eta_mask])), "round-trip 後無效 eta 格點必須維持 NaN")
    restored_pc = factors.singular_values[:, None] * factors.right_singular_vectors_time
    pc_difference = np.abs(restored_pc - np.asarray(source.pc, dtype=np.float64))
    left_orthogonality = float(
        np.max(
            np.abs(
                factors.left_singular_vectors_weighted.T @ factors.left_singular_vectors_weighted
                - np.eye(mode_count)
            )
        )
    )
    right_orthogonality = float(
        np.max(
            np.abs(
                factors.right_singular_vectors_time @ factors.right_singular_vectors_time.T
                - np.eye(mode_count)
            )
        )
    )
    return {
        "max_abs_difference_mode_u_mps_per_raw_pc": float(np.max(u_difference)),
        "max_abs_difference_mode_v_mps_per_raw_pc": float(np.max(v_difference)),
        "max_abs_difference_mode_eta_m_per_raw_pc": float(np.max(eta_difference)),
        "max_abs_difference_pc": float(np.max(pc_difference)),
        "left_singular_vector_orthogonality_max_abs_error": left_orthogonality,
        "right_singular_vector_orthogonality_max_abs_error": right_orthogonality,
    }


def _write_feature_index_map(path: Path, source: HostLayoutSource, factors: HostLayoutFactors) -> None:
    """寫出主持人版 feature 行號與 layer/grid 實體位置的可讀 CSV 對照表。

    這份表是由矩陣回填左圖的唯一公開索引契約：第一欄是 A 的 row index，後續欄位指出
    eta/u/v、速度深度、規則格網 row/col、lon/lat、物理 mean 與加權尺度。編號同時提供
    0-based（程式）與 1-based（主持人圖面）版本，避免答辯時因編號基準不同而誤判格點。
    """

    component_names = {0: "eta", 1: "u", 2: "v"}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "host_feature_index_zero_based",
                "host_feature_index_one_based",
                "matrix_axis",
                "component",
                "velocity_level_id",
                "depth_m_below_surface",
                "grid_row_zero_based",
                "grid_col_zero_based",
                "grid_row_one_based",
                "grid_col_one_based",
                "longitude_degrees_east",
                "latitude_degrees_north",
                "physical_mean",
                "feature_scale",
            ),
        )
        writer.writeheader()
        for index in range(factors.feature_count):
            component = int(factors.feature_component[index])
            level_index = int(factors.feature_level_index[index])
            row = int(factors.feature_row[index])
            col = int(factors.feature_col[index])
            if level_index < 0:
                level_id = "eta_surface"
                depth_m = 0.0
            else:
                level_id = VELOCITY_LEVEL_IDS[level_index]
                depth_m = float(VELOCITY_LEVEL_DEPTHS_M[level_index])
            writer.writerow(
                {
                    "host_feature_index_zero_based": index,
                    "host_feature_index_one_based": index + 1,
                    "matrix_axis": "row_of_A_feature_by_time",
                    "component": component_names[component],
                    "velocity_level_id": level_id,
                    "depth_m_below_surface": depth_m,
                    "grid_row_zero_based": row,
                    "grid_col_zero_based": col,
                    "grid_row_one_based": row + 1,
                    "grid_col_one_based": col + 1,
                    "longitude_degrees_east": float(source.lon[col]),
                    "latitude_degrees_north": float(source.lat[row]),
                    "physical_mean": float(factors.feature_mean_physical[index]),
                    "feature_scale": float(factors.feature_scale[index]),
                }
            )


def export_water_column_host_layout(*, run_dir: Path, output_root: Path) -> Path:
    """從已發布的水柱 SVD 匯出主持人指定矩陣表示與可回填格網的 SVD 因子。

    來源科學 run 使用 ``X=(time,feature)``；本函式發布 ``A=P@X.T`` 的因子，其中 rows 為
    ``eta -> all u depths -> all v depths``，columns 為 UTC time。它不重新呼叫 solver，原因
    不是省略 SVD，而是 ``A`` 的 SVD 可由已驗證直接 SVD 以嚴格矩陣恆等式得到。metadata 同時
    保存此恆等式、來源 residual 對調關係與 round-trip 數值證據，讓任何人可由 compact 左奇異
    向量和 CSV map 重畫左側空間圖。
    """

    resolved_run_dir = run_dir.resolve()
    resolved_output_root = output_root.resolve()
    _require(resolved_run_dir.is_dir(), f"來源 run 不存在: {resolved_run_dir}")
    _require(not resolved_output_root.is_relative_to(resolved_run_dir), "主持人版輸出不可寫入來源 immutable run 內")
    source_paths = tuple(resolved_run_dir / filename for filename in HOST_LAYOUT_REQUIRED_SOURCE_FILENAMES)
    source_hashes_before = {str(path.relative_to(resolved_run_dir)): _sha256_file(path) for path in source_paths}
    source = _load_host_layout_source(resolved_run_dir)
    final_dir = resolved_output_root / "water_column_svd_host_layout" / resolved_run_dir.name / HOST_LAYOUT_ID
    if final_dir.exists():
        raise FileExistsError(f"主持人版矩陣表示已發布，拒絕覆寫: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    partial_dir = final_dir.parent / f".{HOST_LAYOUT_ID}.partial-{uuid.uuid4().hex}"
    partial_dir.mkdir(parents=False)
    try:
        factors = _build_host_layout_factors(source)
        # 先以主持人版 U_A 與 index map 實際回填圖場；以下寫出的 NPY 正是答辯時可直接
        # 畫成左側六層 u/v、一次 eta 圖的資料，不是從來源圖檔複製而來。
        restored_u, restored_v, restored_eta = reconstruct_host_layout_physical_modes(source, factors)
        roundtrip = _roundtrip_validation(
            source,
            factors,
            restored_u=restored_u,
            restored_v=restored_v,
            restored_eta=restored_eta,
        )
        roundtrip_tolerance = 1.0e-12
        _require(
            max(
                roundtrip["max_abs_difference_mode_u_mps_per_raw_pc"],
                roundtrip["max_abs_difference_mode_v_mps_per_raw_pc"],
                roundtrip["max_abs_difference_mode_eta_m_per_raw_pc"],
                roundtrip["max_abs_difference_pc"],
            ) <= roundtrip_tolerance,
            "主持人版向量無法在 1e-12 內 round-trip 回來源空間模態或 PC",
        )

        np.save(partial_dir / "left_singular_vectors_weighted.npy", factors.left_singular_vectors_weighted, allow_pickle=False)
        np.save(partial_dir / "right_singular_vectors_time.npy", factors.right_singular_vectors_time, allow_pickle=False)
        np.save(partial_dir / "singular_values.npy", factors.singular_values, allow_pickle=False)
        np.save(partial_dir / "pc.npy", factors.pc, allow_pickle=False)
        np.save(partial_dir / "feature_scale.npy", factors.feature_scale, allow_pickle=False)
        np.save(partial_dir / "feature_mean_physical.npy", factors.feature_mean_physical, allow_pickle=False)
        np.save(partial_dir / "time_utc_ns.npy", source.time_utc_ns, allow_pickle=False)
        np.save(partial_dir / "roundtrip_mode_u_mps_per_raw_pc.npy", restored_u, allow_pickle=False)
        np.save(partial_dir / "roundtrip_mode_v_mps_per_raw_pc.npy", restored_v, allow_pickle=False)
        np.save(partial_dir / "roundtrip_mode_eta_m_per_raw_pc.npy", restored_eta, allow_pickle=False)
        _write_feature_index_map(partial_dir / "feature_index_map.csv", source, factors)

        source_svd = source.metadata["svd"]
        source_orientation = str(source_svd.get("matrix_orientation", "time_by_feature"))
        host_is_source_orientation = source_orientation == "feature_by_time"
        matrix_shape = [factors.feature_count, int(source.time_utc_ns.size)]
        source_hashes_after = {str(path.relative_to(resolved_run_dir)): _sha256_file(path) for path in source_paths}
        _require(source_hashes_after == source_hashes_before, "主持人版輸出期間來源 immutable run 發生變動")
        metadata = {
            "schema_name": "ocm_water_column_host_layout_svd",
            "schema_version": HOST_LAYOUT_SCHEMA_VERSION,
            "status": "host_layout_complete",
            "layout_id": HOST_LAYOUT_ID,
            "source_run": {
                "run_dir": str(resolved_run_dir),
                "analysis_label": resolved_run_dir.name,
                "source_files_sha256": source_hashes_before,
                "source_modified": False,
                "native_cache_read": False,
                "surface_cache_read": False,
                "svd_solver_called": False,
            },
            "matrix": {
                "symbol": "A",
                "shape": matrix_shape,
                "orientation": "feature_by_time",
                "rows": "host state-vector features",
                "columns": "retained UTC time samples",
                "state_vector_order": [
                    "eta_surface_once",
                    "u_surface_then_z010_z020_z030_z040_z050",
                    "v_surface_then_z010_z020_z030_z040_z050",
                ],
                "feature_index_map": "feature_index_map.csv",
                "invalid_value_policy": "land/depth-invalid/NaN-excluded features have no compact row; re-expanded positions remain NaN",
                "physical_reconstruction": "q_hat_physical(:,t) = feature_mean_physical + (left_singular_vectors_weighted / feature_scale[:,None]) @ pc[:,t]",
            },
            "svd": {
                "factorization": "A_r = U_A[:, :r] @ Sigma_r @ Vh_A[:r, :]",
                "derivation": (
                    "來源已是主持人方向 A=feature×time，直接採用來源因子"
                    if host_is_source_orientation
                    else "舊版來源 X=time×feature，使用 A=P@X.T 的精確轉置與 feature 重排"
                ),
                "method": (
                    "identity_host_orientation_from_source_direct_svd"
                    if host_is_source_orientation
                    else "exact_transpose_and_feature_permutation_of_source_direct_svd"
                ),
                "source_matrix_orientation": source_orientation,
                "source_direct_solver": source_svd.get("solver"),
                "mode_count": int(factors.singular_values.size),
                "singular_values_file": "singular_values.npy",
                "left_singular_vectors_weighted_file": "left_singular_vectors_weighted.npy",
                "right_singular_vectors_time_file": "right_singular_vectors_time.npy",
                "pc_file": "pc.npy",
                "source_left_relative_residuals": source_svd.get("left_relative_residuals"),
                "source_right_relative_residuals": source_svd.get("right_relative_residuals"),
                "host_left_relative_residuals": (
                    source_svd.get("left_relative_residuals")
                    if host_is_source_orientation
                    else source_svd.get("right_relative_residuals")
                ),
                "host_right_relative_residuals": (
                    source_svd.get("right_relative_residuals")
                    if host_is_source_orientation
                    else source_svd.get("left_relative_residuals")
                ),
                "source_orthogonality_max_abs_error": source_svd.get("orthogonality_max_abs_error"),
            },
            "roundtrip_validation": {
                **roundtrip,
                "absolute_tolerance": roundtrip_tolerance,
                "meaning": "由主持人版 compact 左奇異向量解除尺度並依 feature map 散回格網，與來源 mode_u/mode_v/mode_eta/pc 比較",
            },
            "arrays": {
                "left_singular_vectors_weighted.npy": {"shape": list(factors.left_singular_vectors_weighted.shape), "dimensions": ["feature", "mode"]},
                "right_singular_vectors_time.npy": {"shape": list(factors.right_singular_vectors_time.shape), "dimensions": ["mode", "time"]},
                "singular_values.npy": {"shape": list(factors.singular_values.shape), "dimensions": ["mode"]},
                "pc.npy": {"shape": list(factors.pc.shape), "dimensions": ["mode", "time"]},
                "feature_scale.npy": {"shape": list(factors.feature_scale.shape), "dimensions": ["feature"]},
                "feature_mean_physical.npy": {"shape": list(factors.feature_mean_physical.shape), "dimensions": ["feature"]},
                "time_utc_ns.npy": {"shape": list(source.time_utc_ns.shape), "dimensions": ["time"]},
                "roundtrip_mode_u_mps_per_raw_pc.npy": {"shape": list(restored_u.shape), "dimensions": ["mode", "velocity_level", "lat", "lon"]},
                "roundtrip_mode_v_mps_per_raw_pc.npy": {"shape": list(restored_v.shape), "dimensions": ["mode", "velocity_level", "lat", "lon"]},
                "roundtrip_mode_eta_m_per_raw_pc.npy": {"shape": list(restored_eta.shape), "dimensions": ["mode", "lat", "lon"]},
            },
            "provenance_sha256": _canonical_json_hash(
                {
                    "source_run": resolved_run_dir.name,
                    "source_hashes": source_hashes_before,
                    "layout_id": HOST_LAYOUT_ID,
                    "matrix_shape": matrix_shape,
                    "roundtrip": roundtrip,
                }
            ),
        }
        _write_json(partial_dir / "metadata.json", metadata)
        _write_json(partial_dir / "config.json", source.config.raw)
        partial_dir.replace(final_dir)
        return final_dir
    except BaseException:
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
