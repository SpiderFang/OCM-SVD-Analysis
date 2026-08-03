"""由 OCM native `hvel/zcor` 建立固定物理深度流速—海面高度聯合 SVD。

本模組延續既有表層 `u/v/eta` 分析，而不是另做不相容的純流速產品。每一個核定深度
都以逐時、逐 source node 的 `zcor` 找到上下包夾層，僅在可內插時求得 `u(z), v(z)`；
海面高度則直接讀取同月 `ocm_surface/eta_m.npy`。`eta` 是唯一的二維自由水面場，
不做垂向內插，也不得被描述成某個深度的海面高度。

為了讓表層、-5 m、-10 m 與 -20 m 的模態可作垂向比較，本管線使用所有層位共同的
水平有效格點交集與時間樣本交集。表層參考結果會另存於同一 immutable family run，
但不覆寫既有完整 272 格表層成果；前者服務同遮罩垂向比較，後者仍服務完整表層描述。
"""

from __future__ import annotations

import os
import shutil
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .performance import PerformanceRecorder
from .surface_multivariate_svd import (
    AnalysisConfig,
    PreparedAnalysisData,
    SourceMonth,
    _academic_visualization_fields,
    _apply_known_time_axis_repairs,
    _array_descriptor,
    _canonical_json_hash,
    _configured_year_label,
    _expand_imputed_mask,
    _expand_ocean_values,
    _interpolate_short_gaps,
    _iso_utc_from_ns,
    _load_grid,
    _read_json_object,
    _require,
    _solution_physical_loadings,
    _validate_month_metadata,
    _validate_source_time_axis,
    _write_json,
    load_analysis_config,
    solve_surface_multivariate_svd,
)


FIXED_DEPTH_ANALYSIS_KIND = "fixed_depth_multivariate_svd"
"""固定深度 family 設定必須使用的 analysis kind，避免與表層 run 混淆。"""

FIXED_DEPTH_VARIABLES = ("u_fixed_depth_mps", "v_fixed_depth_mps", "eta_m")
"""每個固定深度聯合 SVD 的三個變數；`eta_m` 始終代表自由水面。"""


@dataclass(frozen=True)
class FixedDepthConfig:
    """固定深度 family 經驗證後的設定。

    `depths_m` 是正值的「基準面以下深度」，實際垂向內插目標為 `z=-depth`。本版本只
    接受 `intersection_with_surface`，表示表層參考與所有固定深度共用格點、時間樣本；
    若未來要最大化單一深度覆蓋率，必須新增不同 policy 與 run version，不可靜默改變。
    """

    base: AnalysisConfig
    depths_m: tuple[float, ...]
    comparison_mask_policy: str


@dataclass(frozen=True)
class FixedDepthMonthChunk:
    """單月表層參考與固定深度衍生場。

    `fields` 維度為 `(level, time, component, lat, lon)`；level 0 是表層參考，其後依設定
    深度排序。`valid` 是每個 level 的三變數共同有限遮罩。`bracket_span_m` 只涵蓋固定
    深度，記錄上下 `zcor` 包夾層距離，供判斷垂向內插解析度；無法內插處維持 NaN。
    """

    year: int
    month: int
    native_source: SourceMonth
    surface_source: SourceMonth
    time_utc_ns: np.ndarray
    fields: np.ndarray
    valid: np.ndarray
    bracket_span_m: np.ndarray
    repaired_time_step_count: int


@dataclass(frozen=True)
class LoadedFixedDepthFamily:
    """串接所有月份後、尚未套用共同分析遮罩的垂向比較 family。"""

    lon: np.ndarray
    lat: np.ndarray
    cell_area_m2: np.ndarray
    mask_static: np.ndarray
    analysis_geometry_mask: np.ndarray
    level_ids: tuple[str, ...]
    level_contexts_zh: tuple[str, ...]
    depths_m: tuple[float, ...]
    fields: np.ndarray
    valid: np.ndarray
    bracket_span_m: np.ndarray
    time_utc_ns: np.ndarray
    lat_slice: slice
    lon_slice: slice
    native_sources: tuple[SourceMonth, ...]
    surface_sources: tuple[SourceMonth, ...]
    repaired_time_step_count: int


@dataclass(frozen=True)
class PreparedFixedDepthFamily:
    """具有完全一致空間與時間樣本的表層／固定深度 SVD 輸入。"""

    levels: tuple[PreparedAnalysisData, ...]
    common_mask: np.ndarray
    complete_time_mask: np.ndarray
    level_cell_valid_fraction: np.ndarray


def _as_positive_depth(value: object, field: str) -> float:
    """把設定深度驗證為有限正值，防止把 z 座標正負號混入使用者介面。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必須是正數")
    depth = float(value)
    if not np.isfinite(depth) or depth <= 0.0:
        raise ValueError(f"{field} 必須是有限正數")
    return depth


def load_fixed_depth_config(config_path: Path) -> FixedDepthConfig:
    """讀取固定深度 `u(z)/v(z)/eta` family 設定並驗證垂向比較政策。"""

    base = load_analysis_config(
        config_path,
        expected_analysis_kind=FIXED_DEPTH_ANALYSIS_KIND,
        expected_variables=FIXED_DEPTH_VARIABLES,
    )
    fixed = base.raw.get("fixed_depth")
    if not isinstance(fixed, dict):
        raise ValueError("fixed_depth 必須是物件")
    raw_depths = fixed.get("depths_m_below_vertical_datum")
    if not isinstance(raw_depths, list) or not raw_depths:
        raise ValueError("fixed_depth.depths_m_below_vertical_datum 必須是非空白清單")
    depths = tuple(
        _as_positive_depth(value, f"fixed_depth.depths_m_below_vertical_datum[{index}]")
        for index, value in enumerate(raw_depths)
    )
    _require(len(depths) == len(set(depths)), "固定深度不可重複")
    _require(tuple(sorted(depths)) == depths, "固定深度必須由淺到深排序")
    policy = fixed.get("comparison_mask_policy")
    _require(
        policy == "intersection_with_surface",
        "fixed_depth.comparison_mask_policy 目前必須是 intersection_with_surface",
    )
    _require(
        fixed.get("vertical_interpolation") == "linear_between_bracketing_finite_zcor_no_extrapolation",
        "fixed_depth.vertical_interpolation 必須明確禁止垂向外插",
    )
    _require(
        fixed.get("eta_source") == "paired_ocm_surface_eta_m",
        "fixed_depth.eta_source 必須是 paired_ocm_surface_eta_m",
    )
    _require(
        fixed.get("eta_vertical_interpolation") is False,
        "fixed_depth.eta_vertical_interpolation 必須是 false；eta 沒有垂向層",
    )
    _require(
        fixed.get("include_surface_reference") is True,
        "fixed_depth.include_surface_reference 必須是 true，才能建立同遮罩表層基準",
    )
    _require(
        fixed.get("target_z_formula")
        == "target_z_m = -depth_m_below_vertical_datum",
        "fixed_depth.target_z_formula 必須明載正深度轉成負 z 的公式",
    )
    return FixedDepthConfig(base=base, depths_m=depths, comparison_mask_policy=policy)


def interpolate_velocity_to_fixed_z(
    hvel: np.ndarray,
    zcor: np.ndarray,
    target_z_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把 source-node 水平速度線性內插至固定物理 `z`，絕不外插。

    輸入 `hvel` 為 `(time, source_node, layer, component)`，`zcor` 為相同前三維；
    每一柱只使用 `zcor/u/v` 同時有限的層。演算法不假設 layer index 的有效範圍或排序，
    而是在每柱找 `target_z_m` 下方最接近的層與上方最接近的層。若缺任一包夾層，
    `u/v/bracket_span` 都輸出 NaN；若目標恰落在有效層上，直接取該層且 span 為 0。
    """

    _require(
        hvel.ndim == 4 and hvel.shape[-1] >= 2,
        "固定深度內插的 hvel 必須是 (time,node,layer,component>=2)",
    )
    _require(zcor.shape == hvel.shape[:3], "zcor 必須與 hvel 的 time/node/layer 完全對齊")
    _require(np.isfinite(target_z_m), "固定深度 target_z_m 必須有限")

    velocity = np.asarray(hvel[..., :2], dtype=np.float64)
    physical_z = np.asarray(zcor, dtype=np.float64)
    usable = np.isfinite(physical_z) & np.all(np.isfinite(velocity), axis=-1)
    below_candidates = usable & (physical_z <= target_z_m)
    above_candidates = usable & (physical_z >= target_z_m)
    has_bracket = np.any(below_candidates, axis=-1) & np.any(above_candidates, axis=-1)

    below_index = np.argmax(np.where(below_candidates, physical_z, -np.inf), axis=-1)
    above_index = np.argmin(np.where(above_candidates, physical_z, np.inf), axis=-1)
    z_below = np.take_along_axis(physical_z, below_index[..., None], axis=-1)[..., 0]
    z_above = np.take_along_axis(physical_z, above_index[..., None], axis=-1)[..., 0]
    uv_below = np.take_along_axis(
        velocity,
        below_index[..., None, None],
        axis=2,
    )[..., 0, :]
    uv_above = np.take_along_axis(
        velocity,
        above_index[..., None, None],
        axis=2,
    )[..., 0, :]

    span = z_above - z_below
    exact = has_bracket & (np.abs(span) <= np.finfo(np.float64).eps * 16.0)
    nonzero = has_bracket & ~exact
    result = np.full((*physical_z.shape[:2], 2), np.nan, dtype=np.float64)
    result[exact] = uv_below[exact]
    fraction = np.zeros_like(span)
    fraction[nonzero] = (target_z_m - z_below[nonzero]) / span[nonzero]
    result[nonzero] = uv_below[nonzero] + fraction[nonzero, None] * (
        uv_above[nonzero] - uv_below[nonzero]
    )
    bracket_span = np.where(has_bracket, span, np.nan)
    return result[..., 0], result[..., 1], bracket_span


def _horizontal_barycentric_interpolate(
    node_values: np.ndarray,
    local_vertices: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """以 surface grid 已發布的 source vertices/weights 將 node 場內插到 focus 小窗。

    `node_values` 是 `(time, selected_source_node)`；`local_vertices` 與 `weights` 是
    `(lat, lon, 3)`。三個支撐值必須全有限，否則該格保留 NaN；這與現有表層前處理的
    Delaunay 重心內插缺值政策一致，避免跨無資料頂點創造假流速或假海面高度。
    """

    _require(node_values.ndim == 2, "水平內插 node_values 必須是 (time, selected_source_node)")
    _require(
        local_vertices.ndim == 3
        and local_vertices.shape[-1] == 3
        and weights.shape == local_vertices.shape,
        "水平內插 vertices/weights 必須是 (lat,lon,3)",
    )
    supported = np.all(local_vertices >= 0, axis=-1) & np.all(np.isfinite(weights), axis=-1)
    safe_vertices = np.where(local_vertices >= 0, local_vertices, 0)
    gathered = node_values[:, safe_vertices]
    finite = supported[None, ...] & np.all(np.isfinite(gathered), axis=-1)
    result = np.full((node_values.shape[0], *supported.shape), np.nan, dtype=np.float64)
    weighted = np.sum(gathered * weights[None, ...], axis=-1)
    result[finite] = weighted[finite]
    return result


def _load_fixed_depth_grid(
    surface_root: Path,
    native_root: Path,
    config: FixedDepthConfig,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    slice,
    slice,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """載入規則格網與 native local-node 對應，只保留 focus 所需 source nodes。"""

    (
        lon,
        lat,
        area,
        static,
        geometry,
        lat_slice,
        lon_slice,
    ) = _load_grid(surface_root, config.base)
    surface_grid_dir = surface_root / config.base.domain_id / "grid"
    native_grid_dir = native_root / config.base.domain_id / "grid"
    if not native_grid_dir.is_dir():
        raise FileNotFoundError(f"找不到 native grid 目錄: {native_grid_dir}")
    vertices_all = np.load(
        surface_grid_dir / "source_vertices.npy",
        mmap_mode="r",
        allow_pickle=False,
    )
    weights_all = np.load(
        surface_grid_dir / "source_weights.npy",
        mmap_mode="r",
        allow_pickle=False,
    )
    source_global = np.load(
        native_grid_dir / "source_node_global_index.npy",
        mmap_mode="r",
        allow_pickle=False,
    )
    vertices = np.asarray(vertices_all[lat_slice, lon_slice], dtype=np.int64)
    weights = np.asarray(weights_all[lat_slice, lon_slice], dtype=np.float64)
    _require(vertices.shape == (*static.shape, 3), "source_vertices focus shape 必須是 (lat,lon,3)")
    _require(weights.shape == vertices.shape, "source_weights 必須與 source_vertices 對齊")
    supported_vertices = vertices[vertices >= 0]
    _require(supported_vertices.size > 0, "focus bbox 沒有任何水平內插支撐節點")
    _require(
        int(np.max(supported_vertices)) < int(source_global.size),
        "surface source_vertices 超出 paired native local source node 範圍",
    )
    selected_nodes = np.unique(supported_vertices)
    local_lookup = np.full(source_global.size, -1, dtype=np.int64)
    local_lookup[selected_nodes] = np.arange(selected_nodes.size, dtype=np.int64)
    local_vertices = np.full(vertices.shape, -1, dtype=np.int64)
    valid_vertex = vertices >= 0
    local_vertices[valid_vertex] = local_lookup[vertices[valid_vertex]]
    return (
        lon,
        lat,
        area,
        static,
        geometry,
        lat_slice,
        lon_slice,
        selected_nodes,
        local_vertices,
        weights,
    )


def _open_required_array(directory: Path, filename: str, month_id: str) -> np.ndarray:
    """以唯讀 memory-map 開啟月份必要欄位，缺檔時提供可定位的錯誤。"""

    path = directory / filename
    if not path.is_file():
        raise FileNotFoundError(f"{month_id} 缺少必要欄位: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _load_one_fixed_depth_month(
    *,
    year: int,
    month: int,
    config: FixedDepthConfig,
    surface_root: Path,
    native_root: Path,
    lat_slice: slice,
    lon_slice: slice,
    selected_nodes: np.ndarray,
    local_vertices: np.ndarray,
    weights: np.ndarray,
    grid_full_lat: int,
    grid_full_lon: int,
    allow_partial_months: bool,
    allow_trial: bool,
) -> FixedDepthMonthChunk:
    """讀取一個 paired native/surface 月份並同時計算所有固定深度。

    同月的 `hvel/zcor` 只裁切一次 selected source nodes，再依多個目標深度重用；因此
    增加 -5/-10/-20 m 不會重讀三次完整 native cache。surface `eta_m` 與表層參考
    `u/v` 直接取 focus 小窗，並要求 raw time 軸與 native 完全相同。
    """

    month_id = f"{year}{month:02d}"
    surface_month = surface_root / config.base.domain_id / "months" / month_id
    native_month = native_root / config.base.domain_id / "months" / month_id
    surface_metadata = _read_json_object(surface_month / "metadata.json")
    native_metadata = _read_json_object(native_month / "metadata.json")
    surface_kind = _validate_month_metadata(
        surface_metadata,
        config.base,
        month_id,
        allow_partial_months=allow_partial_months,
        allow_trial=allow_trial,
    )
    native_kind = _validate_month_metadata(
        native_metadata,
        config.base,
        month_id,
        allow_partial_months=allow_partial_months,
        allow_trial=allow_trial,
    )
    _require(surface_kind == native_kind, f"{month_id} paired surface/native cache kind 不一致")
    _require(
        surface_metadata.get("config_hash") == native_metadata.get("config_hash"),
        f"{month_id} paired surface/native config_hash 不一致",
    )

    surface_time = _open_required_array(surface_month, "time_utc_ns.npy", month_id)
    native_time = _open_required_array(native_month, "time_utc_ns.npy", month_id)
    _require(
        surface_time.dtype == np.int64
        and native_time.dtype == np.int64
        and surface_time.ndim == 1
        and np.array_equal(surface_time, native_time),
        f"{month_id} paired surface/native time_utc_ns 必須逐值相同",
    )
    repaired_time, repaired_count = _apply_known_time_axis_repairs(
        surface_time,
        config.base,
        month_id,
    )

    u_surface = _open_required_array(surface_month, "u_surface_mps.npy", month_id)
    v_surface = _open_required_array(surface_month, "v_surface_mps.npy", month_id)
    eta = _open_required_array(surface_month, "eta_m.npy", month_id)
    valid_surface = _open_required_array(
        surface_month,
        "valid_mask_surface.npy",
        month_id,
    )
    expected_surface_shape = (surface_time.size, grid_full_lat, grid_full_lon)
    for name, array in (
        ("u_surface_mps.npy", u_surface),
        ("v_surface_mps.npy", v_surface),
        ("eta_m.npy", eta),
    ):
        _require(
            array.shape == expected_surface_shape
            and np.issubdtype(array.dtype, np.floating),
            f"{month_id} {name} 必須是與規則格網對齊的浮點 (time,lat,lon)",
        )
    _require(
        valid_surface.dtype == np.bool_
        and valid_surface.shape == expected_surface_shape,
        f"{month_id} valid_mask_surface.npy shape/dtype 不正確",
    )

    hvel = _open_required_array(native_month, "hvel.npy", month_id)
    zcor = _open_required_array(native_month, "zcor.npy", month_id)
    _require(
        hvel.ndim == 4
        and hvel.shape[0] == surface_time.size
        and hvel.shape[-1] >= 2
        and np.issubdtype(hvel.dtype, np.floating),
        f"{month_id} hvel 必須是浮點 (time,node,layer,component>=2)",
    )
    _require(
        zcor.shape == hvel.shape[:3] and np.issubdtype(zcor.dtype, np.floating),
        f"{month_id} zcor 必須與 hvel 的 time/node/layer 對齊",
    )
    _require(
        selected_nodes[-1] < hvel.shape[1],
        f"{month_id} selected source node 超出 native node 維度",
    )

    # NumPy fancy indexing 只 materialize focus 水平插值真正需要的 source nodes；月份
    # worker 不保留完整東北台灣 3D domain，控制並行 I/O 的 RAM 與磁碟頁面壓力。
    selected_hvel = np.asarray(hvel[:, selected_nodes, :, :2], dtype=np.float64)
    selected_zcor = np.asarray(zcor[:, selected_nodes, :], dtype=np.float64)
    eta_focus = np.asarray(eta[:, lat_slice, lon_slice], dtype=np.float64)
    surface_fields = np.stack(
        (
            np.asarray(u_surface[:, lat_slice, lon_slice], dtype=np.float64),
            np.asarray(v_surface[:, lat_slice, lon_slice], dtype=np.float64),
            eta_focus,
        ),
        axis=1,
    )
    surface_valid_focus = np.asarray(
        valid_surface[:, lat_slice, lon_slice],
        dtype=bool,
    ) & np.all(np.isfinite(surface_fields), axis=1)

    level_fields = [surface_fields]
    level_valid = [surface_valid_focus]
    bracket_grids: list[np.ndarray] = []
    for depth_m in config.depths_m:
        u_nodes, v_nodes, bracket_nodes = interpolate_velocity_to_fixed_z(
            selected_hvel,
            selected_zcor,
            -depth_m,
        )
        u_grid = _horizontal_barycentric_interpolate(
            u_nodes,
            local_vertices,
            weights,
        )
        v_grid = _horizontal_barycentric_interpolate(
            v_nodes,
            local_vertices,
            weights,
        )
        bracket_grid = _horizontal_barycentric_interpolate(
            bracket_nodes,
            local_vertices,
            weights,
        )
        fields = np.stack((u_grid, v_grid, eta_focus), axis=1)
        valid = np.all(np.isfinite(fields), axis=1)
        level_fields.append(fields)
        level_valid.append(valid)
        bracket_grids.append(bracket_grid)

    return FixedDepthMonthChunk(
        year=year,
        month=month,
        native_source=SourceMonth(
            month_id,
            native_kind,
            _canonical_json_hash(native_metadata),
            native_metadata,
        ),
        surface_source=SourceMonth(
            month_id,
            surface_kind,
            _canonical_json_hash(surface_metadata),
            surface_metadata,
        ),
        time_utc_ns=repaired_time,
        fields=np.stack(level_fields, axis=0),
        valid=np.stack(level_valid, axis=0),
        bracket_span_m=np.stack(bracket_grids, axis=0),
        repaired_time_step_count=repaired_count,
    )


def load_fixed_depth_family(
    surface_root: Path,
    native_root: Path,
    config: FixedDepthConfig,
    *,
    allow_partial_months: bool,
    allow_trial: bool,
) -> LoadedFixedDepthFamily:
    """平行讀取 paired 月份並建立表層加所有固定深度的垂向比較 family。"""

    (
        lon,
        lat,
        area,
        static,
        geometry,
        lat_slice,
        lon_slice,
        selected_nodes,
        local_vertices,
        weights,
    ) = _load_fixed_depth_grid(surface_root, native_root, config)
    surface_domain = surface_root / config.base.domain_id
    grid_full_lat = int(
        np.load(
            surface_domain / "grid" / "lat.npy",
            mmap_mode="r",
            allow_pickle=False,
        ).size
    )
    grid_full_lon = int(
        np.load(
            surface_domain / "grid" / "lon.npy",
            mmap_mode="r",
            allow_pickle=False,
        ).size
    )
    pairs = tuple(
        (year, month)
        for year in config.base.years
        for month in config.base.months
    )
    worker_count = min(config.base.io_workers, len(pairs))
    common_arguments = {
        "config": config,
        "surface_root": surface_root,
        "native_root": native_root,
        "lat_slice": lat_slice,
        "lon_slice": lon_slice,
        "selected_nodes": selected_nodes,
        "local_vertices": local_vertices,
        "weights": weights,
        "grid_full_lat": grid_full_lat,
        "grid_full_lon": grid_full_lon,
        "allow_partial_months": allow_partial_months,
        "allow_trial": allow_trial,
    }
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="ocm-fixed-z-io",
    ) as executor:
        futures = {
            pair: executor.submit(
                _load_one_fixed_depth_month,
                year=pair[0],
                month=pair[1],
                **common_arguments,
            )
            for pair in pairs
        }
        chunks = [futures[pair].result() for pair in pairs]

    time = np.concatenate([chunk.time_utc_ns for chunk in chunks], axis=0)
    _require(
        np.all(np.diff(time) > 0),
        "固定深度跨月 time_utc_ns 必須嚴格遞增且不可有重複邊界",
    )
    level_ids = ("surface_reference",) + tuple(
        f"z_minus_{depth_m:06.2f}m".replace(".", "p")
        for depth_m in config.depths_m
    )
    level_contexts = ("共同遮罩表層流",) + tuple(
        f"固定深度 -{depth_m:g} m 流"
        for depth_m in config.depths_m
    )
    return LoadedFixedDepthFamily(
        lon=lon,
        lat=lat,
        cell_area_m2=area,
        mask_static=static,
        analysis_geometry_mask=geometry,
        level_ids=level_ids,
        level_contexts_zh=level_contexts,
        depths_m=config.depths_m,
        fields=np.concatenate([chunk.fields for chunk in chunks], axis=1),
        valid=np.concatenate([chunk.valid for chunk in chunks], axis=1),
        bracket_span_m=np.concatenate(
            [chunk.bracket_span_m for chunk in chunks],
            axis=1,
        ),
        time_utc_ns=time,
        lat_slice=lat_slice,
        lon_slice=lon_slice,
        native_sources=tuple(chunk.native_source for chunk in chunks),
        surface_sources=tuple(chunk.surface_source for chunk in chunks),
        repaired_time_step_count=sum(
            chunk.repaired_time_step_count for chunk in chunks
        ),
    )


def prepare_fixed_depth_family(
    loaded: LoadedFixedDepthFamily,
    config: FixedDepthConfig,
) -> PreparedFixedDepthFamily:
    """建立跨表層與所有固定深度都一致的 common mask 與 retained time。

    每個 level 先依自己的三變數有效率計算 coverage；共同遮罩要求所有 level 都達到
    設定門檻。短缺值只在同一 level、cell 且有前後端點時插補，最後再取所有 level
    皆完整的時間交集。這比逐深度各自刪除時次更保守，但可避免 PC 差異來自樣本集合
    不同，而不是垂向流場差異。
    """

    _validate_source_time_axis(loaded.time_utc_ns, config.base)
    _require(
        loaded.fields.ndim == 5
        and loaded.fields.shape[:2] == loaded.valid.shape[:2]
        and loaded.fields.shape[2] == 3,
        "固定深度 family fields 必須是 (level,time,3,lat,lon)",
    )
    triplet_valid = loaded.valid & np.all(np.isfinite(loaded.fields), axis=2)
    level_fraction = np.mean(triplet_valid, axis=1, dtype=np.float64)
    common_mask = (
        loaded.analysis_geometry_mask
        & loaded.mask_static
        & np.all(
            level_fraction >= config.base.minimum_cell_valid_fraction,
            axis=0,
        )
    )
    common_count = int(np.count_nonzero(common_mask))
    _require(
        common_count >= config.base.minimum_static_ocean_cells,
        f"表層與固定深度共同海域格點只有 {common_count}，"
        f"小於門檻 {config.base.minimum_static_ocean_cells}",
    )
    interpolated_levels: list[np.ndarray] = []
    imputed_levels: list[np.ndarray] = []
    for level_index in range(loaded.fields.shape[0]):
        # 先固定 level，再用二維布林遮罩壓平最後兩個空間軸，可穩定得到
        # `(time, component, ocean_cell)`；若把 rows/cols 直接混入五維進階索引，
        # NumPy 會把 ocean cell 軸移到最前方，容易讓時間與分量軸在無錯誤下互換。
        series = loaded.fields[level_index][:, :, common_mask]
        local_valid = triplet_valid[level_index][:, common_mask]
        _require(
            series.shape
            == (
                loaded.time_utc_ns.size,
                3,
                int(np.count_nonzero(common_mask)),
            ),
            "共同遮罩壓平後的固定深度 series shape 不正確",
        )
        series = np.where(local_valid[:, None, :], series, np.nan)
        interpolated, imputed = _interpolate_short_gaps(
            series,
            config.base.max_interpolation_steps,
        )
        interpolated_levels.append(interpolated)
        imputed_levels.append(imputed)

    complete_time = np.ones(loaded.time_utc_ns.size, dtype=bool)
    for interpolated in interpolated_levels:
        complete_time &= np.all(np.isfinite(interpolated), axis=(1, 2))
    retained_fraction = float(np.mean(complete_time))
    _require(
        retained_fraction >= config.base.minimum_retained_time_fraction,
        f"跨層共同完整時間僅 {retained_fraction:.3%}，"
        f"低於門檻 {config.base.minimum_retained_time_fraction:.3%}",
    )
    _require(
        int(np.count_nonzero(complete_time)) >= 2,
        "跨層共同完整時間不足兩筆，無法執行 SVD",
    )
    prepared_levels = tuple(
        PreparedAnalysisData(
            time_utc_ns=loaded.time_utc_ns[complete_time],
            series=interpolated[complete_time],
            common_mask=common_mask,
            cell_triplet_valid_fraction=level_fraction[level_index],
            imputed=imputed_levels[level_index][complete_time],
            retained_time_fraction=retained_fraction,
            initial_time_count=int(loaded.time_utc_ns.size),
            common_ocean_cell_count=common_count,
        )
        for level_index, interpolated in enumerate(interpolated_levels)
    )
    return PreparedFixedDepthFamily(
        levels=prepared_levels,
        common_mask=common_mask,
        complete_time_mask=complete_time,
        level_cell_valid_fraction=level_fraction,
    )


def _fixed_depth_science_provenance_sha256(
    config: FixedDepthConfig,
    loaded: LoadedFixedDepthFamily,
) -> str:
    """計算固定深度科學內容的完整 SHA-256，供 metadata 稽核。

    目錄名稱刻意不再攜帶雜湊；設定或上游月份內容改變時，研究者必須提升
    `analysis_label` 末尾版本號。完整設定與 paired native/surface metadata 身分仍在
    此欄位保存，因此可讀目錄名不會犧牲來源追溯能力。
    """

    source_signature = [
        {
            "month": native.month_id,
            "native_metadata_sha256": native.metadata_sha256,
            "surface_metadata_sha256": surface.metadata_sha256,
        }
        for native, surface in zip(
            loaded.native_sources,
            loaded.surface_sources,
            strict=True,
        )
    ]
    science_config = {
        key: value
        for key, value in config.base.raw.items()
        if key != "figures"
    }
    return _canonical_json_hash(
        {"config": science_config, "source_months": source_signature}
    )


def _fixed_depth_run_id(config: FixedDepthConfig) -> str:
    """以版本化 `analysis_label` 作固定深度 family 的可讀目錄名稱。

    表層與固定深度分別發布到 `svd/`、`fixed_depth_svd/`，不共用父目錄；即使區域名稱
    相似也不會互相覆寫。名稱必須明確以 `_vN` 結尾，設定或來源改變時由人員升版，而
    不是在目錄尾端附加難以辨識的短雜湊。
    """

    safe_label = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in config.base.analysis_label
    )
    _require(
        re.search(r"_v[1-9][0-9]*$", safe_label) is not None,
        "固定深度 analysis_label 必須以明確版本號 `_vN` 結尾，例如 `_v1`；"
        "科學設定或來源改變時必須升版。",
    )
    return safe_label


def _level_target_depth(
    level_index: int,
    depths_m: tuple[float, ...],
) -> float | None:
    """把 family level index 轉成固定深度；level 0 表層參考回傳 None。"""

    return None if level_index == 0 else depths_m[level_index - 1]


def _fixed_level_array_metadata(
    filename: str,
    array: np.ndarray,
) -> dict[str, object]:
    """建立固定深度 level 陣列的精確維度與單位描述。

    固定深度輸出混合二維平均場、三維模態、四維插補遮罩與一維解釋變異；若只依檔名
    前綴猜測，很容易把 `imputed_mask` 或 `singular_values` 誤標成 `(lat,lon)`。本函式
    集中列出完整契約，使 metadata 可由下游自動驗證，而不是只供人閱讀。
    """

    descriptors: dict[str, tuple[list[str], str]] = {
        "valid_mask.npy": (["lat", "lon"], "bool common-valid analysis ocean mask"),
        "cell_triplet_valid_fraction.npy": (
            ["lat", "lon"],
            "fraction of source times with finite u/v/paired eta at this level",
        ),
        "time_utc_ns.npy": (["time"], "UTC epoch nanoseconds"),
        "imputed_mask.npy": (
            ["time", "component(u,v,eta)", "lat", "lon"],
            "bool; true only where bounded short-gap interpolation was applied",
        ),
        "mean_u.npy": (["lat", "lon"], "m s-1"),
        "mean_v.npy": (["lat", "lon"], "m s-1"),
        "mean_eta.npy": (["lat", "lon"], "m"),
        "mode_u.npy": (["mode", "lat", "lon"], "m s-1 per raw PC unit"),
        "mode_v.npy": (["mode", "lat", "lon"], "m s-1 per raw PC unit"),
        "mode_eta.npy": (["mode", "lat", "lon"], "m per raw PC unit"),
        "pc.npy": (["mode", "time"], "weighted standardized raw PC"),
        "pc_standardized.npy": (
            ["mode", "time"],
            "dimensionless standard deviations; sample std ddof=1",
        ),
        "regression_u.npy": (
            ["mode", "lat", "lon"],
            "m s-1 per 1 standard deviation of corresponding PC",
        ),
        "regression_v.npy": (
            ["mode", "lat", "lon"],
            "m s-1 per 1 standard deviation of corresponding PC",
        ),
        "regression_eta.npy": (
            ["mode", "lat", "lon"],
            "m per 1 standard deviation of corresponding PC",
        ),
        "singular_values.npy": (["mode"], "weighted standardized singular value"),
        "explained_variance.npy": (["mode"], "fraction"),
        "cumulative_explained_variance.npy": (["mode"], "fraction"),
        "all_explained_variance.npy": (["mode"], "fraction"),
        "vertical_bracket_span_m.npy": (
            ["time", "lat", "lon"],
            "m; zcor upper-minus-lower bracketing span after horizontal interpolation",
        ),
    }
    _require(filename in descriptors, f"未定義固定深度輸出陣列 metadata: {filename}")
    dimensions, unit = descriptors[filename]
    return _array_descriptor(array, dimensions, unit)


def run_fixed_depth_multivariate_svd(
    *,
    config_path: Path,
    native_root: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
) -> Path:
    """執行表層參考加多個固定深度的共同遮罩 `u/v/eta` 科學 family。

    輸出父目錄是 `fixed_depth_svd/<analysis_label_vN>/`，其下每個 level 只保存科學
    陣列與 metadata；正式圖面一律由 `ocm-svd-fixed-depth-replot` 發布到獨立的
    `fixed_depth_svd_figure_bundles/`。這項分離避免來源 family 內殘留舊 renderer 圖面，
    也確保不會與 `svd/` 下的完整表層成果混淆或互相覆寫。
    """

    performance = PerformanceRecorder()
    with performance.measure("configuration_and_output_validation"):
        config = load_fixed_depth_config(config_path)
        # 版本名稱在任何大型 paired cache I/O 前驗證，避免運算近兩小時後才因缺少
        # `_vN` 被拒絕；同一可讀版本已存在時仍由下方不可覆寫檢查保護。
        run_id = _fixed_depth_run_id(config)
        native_root = native_root.resolve()
        surface_root = surface_root.resolve()
        output_root = output_root.resolve()
        _require(native_root.is_dir(), f"native_root 不存在或不是目錄: {native_root}")
        _require(surface_root.is_dir(), f"surface_root 不存在或不是目錄: {surface_root}")
        final_dir = output_root / "fixed_depth_svd" / run_id
        if final_dir.exists():
            raise FileExistsError(
                "固定深度 SVD family 版本已存在，為保護表層與固定深度各自的不可變成果"
                f"而拒絕覆寫；請提升 analysis_label 的 `_vN`: {final_dir}"
            )

    with performance.measure("paired_native_surface_month_io_and_vertical_interpolation"):
        loaded = load_fixed_depth_family(
            surface_root,
            native_root,
            config,
            allow_partial_months=allow_partial_months,
            allow_trial=allow_trial,
        )
    with performance.measure("shared_mask_and_missing_data_preparation"):
        prepared_family = prepare_fixed_depth_family(loaded, config)
    # 狀態以 paired cache 中最保守者為準：只要 native 或 surface 任一來源是
    # trial_partial_month，整個垂向比較 family 都只能標成 trial，避免共同遮罩後看似完整的
    # 圖表掩蓋上游仍是部分月份試算資料。候選 AOI 則延續表層 pipeline 的 candidate_pilot。
    family_status = (
        "candidate_pilot"
        if config.base.approval_status == "candidate"
        else "analysis_ready"
    )
    if any(
        source.cache_kind == "trial_partial_month"
        for source in loaded.native_sources + loaded.surface_sources
    ):
        family_status = "trial_pilot"
    with performance.measure("all_level_svd_solver_and_field_derivation"):
        level_products: list[dict[str, Any]] = []
        for level_index, prepared in enumerate(prepared_family.levels):
            solution = solve_surface_multivariate_svd(
                prepared,
                loaded.cell_area_m2,
                config.base,
                loaded.lon,
                loaded.lat,
            )
            mode_u, mode_v, mode_eta = _solution_physical_loadings(
                solution,
                prepared,
                loaded.cell_area_m2,
            )
            visualization = _academic_visualization_fields(
                solution,
                mode_u,
                mode_v,
                mode_eta,
            )
            means = _expand_ocean_values(
                solution.mean_by_component,
                prepared.common_mask,
            )
            level_products.append(
                {
                    "solution": solution,
                    "mode_u": mode_u,
                    "mode_v": mode_v,
                    "mode_eta": mode_eta,
                    "visualization": visualization,
                    "mean_u": means[0],
                    "mean_v": means[1],
                    "mean_eta": means[2],
                }
            )

    science_provenance_sha256 = _fixed_depth_science_provenance_sha256(
        config,
        loaded,
    )
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    partial_dir = final_dir.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
    partial_dir.mkdir(parents=False)
    try:
        # 科學輸出只序列化陣列與 provenance；圖面另由版本化 figure bundle 管線處理。
        with performance.measure("array_and_provenance_serialization"):
            np.save(partial_dir / "lon.npy", loaded.lon, allow_pickle=False)
            np.save(partial_dir / "lat.npy", loaded.lat, allow_pickle=False)
            np.save(
                partial_dir / "cell_area_m2.npy",
                loaded.cell_area_m2,
                allow_pickle=False,
            )
            np.save(
                partial_dir / "analysis_geometry_mask.npy",
                loaded.analysis_geometry_mask,
                allow_pickle=False,
            )
            np.save(
                partial_dir / "shared_valid_mask.npy",
                prepared_family.common_mask,
                allow_pickle=False,
            )
            common_time = prepared_family.levels[0].time_utc_ns
            np.save(
                partial_dir / "time_utc_ns.npy",
                common_time,
                allow_pickle=False,
            )
            _write_json(partial_dir / "config.json", config.base.raw)

            level_summaries: list[dict[str, Any]] = []
            for level_index, product in enumerate(level_products):
                level_id = loaded.level_ids[level_index]
                level_dir = partial_dir / "levels" / level_id
                level_dir.mkdir(parents=True)
                prepared = prepared_family.levels[level_index]
                solution = product["solution"]
                visualization = product["visualization"]
                arrays = {
                    "valid_mask.npy": prepared.common_mask,
                    "cell_triplet_valid_fraction.npy": (
                        prepared.cell_triplet_valid_fraction
                    ),
                    "time_utc_ns.npy": prepared.time_utc_ns,
                    "imputed_mask.npy": _expand_imputed_mask(
                        prepared.imputed,
                        prepared.common_mask,
                    ),
                    "mean_u.npy": product["mean_u"],
                    "mean_v.npy": product["mean_v"],
                    "mean_eta.npy": product["mean_eta"],
                    "mode_u.npy": product["mode_u"],
                    "mode_v.npy": product["mode_v"],
                    "mode_eta.npy": product["mode_eta"],
                    "pc.npy": solution.pc,
                    "pc_standardized.npy": visualization.pc_standardized,
                    "regression_u.npy": visualization.regression_u,
                    "regression_v.npy": visualization.regression_v,
                    "regression_eta.npy": visualization.regression_eta,
                    "singular_values.npy": solution.singular_values,
                    "explained_variance.npy": solution.explained_variance,
                    "cumulative_explained_variance.npy": (
                        solution.cumulative_explained_variance
                    ),
                    "all_explained_variance.npy": solution.all_explained_variance,
                }
                target_depth = _level_target_depth(level_index, config.depths_m)
                if target_depth is not None:
                    bracket_span = loaded.bracket_span_m[
                        level_index - 1,
                        prepared_family.complete_time_mask,
                    ].copy()
                    bracket_span[:, ~prepared.common_mask] = np.nan
                    arrays["vertical_bracket_span_m.npy"] = bracket_span
                for filename, array in arrays.items():
                    np.save(level_dir / filename, array, allow_pickle=False)

                level_metadata = {
                    "schema_name": "ocm_fixed_depth_level_multivariate_svd",
                    "schema_version": "1.0.0",
                    "status": family_status,
                    "family_run_id": run_id,
                    "level_id": level_id,
                    "level_kind": (
                        "surface_reference_on_shared_mask"
                        if target_depth is None
                        else "fixed_physical_depth"
                    ),
                    "target_depth_m_below_vertical_datum": target_depth,
                    "target_z_m": (
                        None if target_depth is None else -target_depth
                    ),
                    "velocity_context_zh": loaded.level_contexts_zh[level_index],
                    "eta_semantics": (
                        "paired ocm_surface eta_m; the unique free-surface "
                        "elevation at the same time and horizontal cell; "
                        "not vertically interpolated"
                    ),
                    "time_window": {
                        "initial_time_count": prepared.initial_time_count,
                        "retained_time_count": int(prepared.time_utc_ns.size),
                        "retained_time_fraction": prepared.retained_time_fraction,
                        "start_utc": _iso_utc_from_ns(
                            int(prepared.time_utc_ns[0])
                        ),
                        "end_utc": _iso_utc_from_ns(
                            int(prepared.time_utc_ns[-1])
                        ),
                    },
                    "mask_and_missing_data": {
                        "comparison_mask_policy": (
                            config.comparison_mask_policy
                        ),
                        "shared_common_ocean_cell_count": (
                            prepared.common_ocean_cell_count
                        ),
                        "minimum_cell_triplet_valid_fraction": (
                            config.base.minimum_cell_valid_fraction
                        ),
                        "triplet_valid_definition": (
                            "finite(level_u) AND finite(level_v) AND "
                            "finite(paired_eta_m) AND mask_static AND "
                            "focus_bbox_cell_center"
                        ),
                        "imputed_scalar_count": int(
                            np.count_nonzero(prepared.imputed)
                        ),
                        "no_zero_fill": True,
                    },
                    "svd": {
                        "variables": list(FIXED_DEPTH_VARIABLES),
                        "state_vector_order": (
                            "[u_1..u_P, v_1..v_P, eta_1..eta_P]"
                        ),
                        "solver": (
                            "spatial_covariance_eigh_equivalent_to_thin_svd"
                        ),
                        "normalization": config.base.raw["svd"][
                            "normalization"
                        ],
                        "area_weight": (
                            "sqrt(cell_area_m2), repeated for u, v, eta"
                        ),
                        "velocity_rms_mps": solution.velocity_rms_mps,
                        "eta_rms_m": solution.eta_rms_m,
                        "mode_count": int(solution.explained_variance.size),
                        "sign_convention": config.base.raw["svd"][
                            "sign_convention"
                        ],
                        "sign_sources": list(solution.sign_sources),
                    },
                    "quality_checks": {
                        "sum_all_explained_variance": float(
                            np.sum(solution.all_explained_variance)
                        ),
                        "spatial_vector_orthogonality_max_abs_error": (
                            solution.orthogonality_max_abs_error
                        ),
                        "full_rank_relative_reconstruction_error": (
                            solution.full_rank_relative_reconstruction_error
                        ),
                        "retained_mode_relative_reconstruction_error": (
                            solution.retained_mode_relative_reconstruction_error
                        ),
                    },
                    "arrays": {
                        filename: _fixed_level_array_metadata(filename, array)
                        for filename, array in arrays.items()
                    },
                    # 科學 family 永遠不內嵌圖面；正式圖包由獨立重繪器依 style 版本發布。
                    "figures": [],
                    "limitations": [
                        "固定深度 u/v 只在逐時 zcor 有上下有限包夾層時線性內插；不做海床以下、海面以上或單側外插。",
                        "eta 是同時次自由水面高度，只作與該層流速的聯合 SVD，不代表 eta 具有垂向層。",
                        "本 family 為垂向比較使用共同格點與時間交集；既有完整表層 run 的 272 格結果仍是完整表層描述，兩者不可混用成同一母體。",
                    ],
                }
                _write_json(level_dir / "metadata.json", level_metadata)
                level_summaries.append(
                    {
                        "level_id": level_id,
                        "level_kind": level_metadata["level_kind"],
                        "target_depth_m_below_vertical_datum": target_depth,
                        "relative_path": str(
                            level_dir.relative_to(partial_dir)
                        ),
                        "mode_count": int(solution.explained_variance.size),
                    }
                )

            parent_metadata = {
                "schema_name": "ocm_fixed_depth_multivariate_svd_family",
                "schema_version": "1.0.0",
                "status": family_status,
                "run_id": run_id,
                "created_at_utc": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "analysis_kind": FIXED_DEPTH_ANALYSIS_KIND,
                "analysis_label": config.base.analysis_label,
                "science_provenance_sha256": science_provenance_sha256,
                "focus": config.base.raw["focus"],
                "year_label": _configured_year_label(config.base),
                "fixed_depth": config.base.raw["fixed_depth"],
                "eta_source": {
                    "array": "paired ocm_surface/<domain>/months/<YYYYMM>/eta_m.npy",
                    "native_origin": "SCHISM elev(time,node) [m]",
                    "vertical_interpolation": False,
                    "horizontal_alignment": (
                        "same published 1 km surface grid and time_utc_ns"
                    ),
                },
                "shared_sample_contract": {
                    "comparison_mask_policy": config.comparison_mask_policy,
                    "common_ocean_cell_count": int(
                        np.count_nonzero(prepared_family.common_mask)
                    ),
                    "retained_time_count": int(common_time.size),
                    "start_utc": _iso_utc_from_ns(int(common_time[0])),
                    "end_utc": _iso_utc_from_ns(int(common_time[-1])),
                    "level_ids": list(loaded.level_ids),
                },
                "source_months": [
                    {
                        "month": native.month_id,
                        "cache_kind": native.cache_kind,
                        "native_metadata_sha256": native.metadata_sha256,
                        "surface_metadata_sha256": surface.metadata_sha256,
                    }
                    for native, surface in zip(
                        loaded.native_sources,
                        loaded.surface_sources,
                        strict=True,
                    )
                ],
                "known_time_axis_repairs": config.base.raw["input"].get(
                    "known_time_axis_repairs",
                    [],
                ),
                "repaired_time_step_count": (
                    loaded.repaired_time_step_count
                ),
                "levels": level_summaries,
                "performance": performance.to_metadata(
                    scope_end=(
                        "從 fixed-depth family 函式入口至父層 metadata "
                        "組裝；不含最後 metadata.json 寫入與原子 rename。"
                    )
                ),
                "limitations": [
                    "focus approval_status=candidate；成果是貢寮候選框 pilot，不取代研究團隊核定 AOI。",
                    "固定 z 的正式垂向 datum 仍須以 OCM 供應者資料契約確認；確認前深度標示屬可重現 pilot 定義。",
                    "近底/HAB 與完整三維 SVD 不在本 family 範圍。",
                ],
            }
            _write_json(partial_dir / "metadata.json", parent_metadata)
        os.replace(partial_dir, final_dir)
    except Exception:
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
    return final_dir
