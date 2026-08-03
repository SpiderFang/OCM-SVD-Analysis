"""從既有固定深度 SVD family 建立最新、不可覆寫的正式圖包。

本模組只讀已發布 family run 內的平均場、回歸空間模態、標準化 PC、時間軸、有效率與
共同遮罩，不讀 paired native/surface cache，也不重新執行垂向內插或 SVD。四個層位
使用表層 `academic_report_ready_v6` 的同一 renderer，因此每張空間圖同時交付：

- 不含比例尺的標準主圖；
- 全透明背景的同模態比例尺素材；
- 已把比例尺放入右下角、可直接用於報告的完整備用圖。

family 根目錄另輸出四分面 coverage/QC 圖，逐層顯示通過年度有效率門檻的格點，並明確
標出候選分析格如何收斂為共同交集。這張圖不會把被排除的沿岸海洋格偽裝成陸地，也不會
回填任何速度或海面高度；特定研究區的實際格數由來源陣列計算，不在程式中硬編碼。
"""

from __future__ import annotations

import inspect
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .fixed_depth_multivariate_svd import (
    FIXED_DEPTH_ANALYSIS_KIND,
    FIXED_DEPTH_VARIABLES,
    load_fixed_depth_config,
)
from .performance import PerformanceRecorder
from .replot import (
    _renderer_code_sha256,
    _renderer_environment_signature,
    _science_config_hash,
    _sha256_file,
)
from .surface_multivariate_svd import (
    AcademicVisualizationFields,
    AnalysisConfig,
    _canonical_json_hash,
    _clip_land_polygons_to_extent,
    _load_geojson_land_polygons,
    _make_figures,
    _read_json_object,
    _require,
    _resolve_report_font,
    _write_json,
)


FIXED_DEPTH_FIGURE_BUNDLE_SCHEMA_VERSION = "1.0.0"
"""固定深度 figure bundle metadata 版本；圖包永遠與來源科學 run 分離。"""


@dataclass(frozen=True)
class FixedDepthLevelFigureInputs:
    """一個層位重繪所需的最小唯讀陣列。

    `visualization` 直接引用既有 `regression_*` 與 `pc_standardized`，不由 raw PC 重新
    回歸。`pc_standard_deviation` 與 `pc_mean_max_abs` 僅由已發布 raw PC 計算作結構
    provenance；它們不會改變空間圖或時間圖的數值。
    """

    mean_u: np.ndarray
    mean_v: np.ndarray
    mean_eta: np.ndarray
    visualization: AcademicVisualizationFields
    time_utc_ns: np.ndarray
    explained_variance: np.ndarray
    cell_triplet_valid_fraction: np.ndarray


def _open_array(directory: Path, filename: str) -> np.ndarray:
    """以唯讀 memory-map 開啟重繪必要陣列，拒絕遺失檔案與 pickle。"""

    path = directory / filename
    if not path.is_file():
        raise FileNotFoundError(f"固定深度重繪來源缺少必要陣列: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _load_level_inputs(
    level_dir: Path,
    *,
    spatial_shape: tuple[int, int],
) -> FixedDepthLevelFigureInputs:
    """載入並驗證單一層位的既有科學陣列。

    shape 契約在畫圖前完整檢查，避免把另一個網格、不同時間軸或不同模態數的檔案混入
    同一 family。所有來源均維持唯讀；重繪器不修改 NaN、共同遮罩或模態正負號。
    """

    mean_u = _open_array(level_dir, "mean_u.npy")
    mean_v = _open_array(level_dir, "mean_v.npy")
    mean_eta = _open_array(level_dir, "mean_eta.npy")
    pc = _open_array(level_dir, "pc.npy")
    pc_standardized = _open_array(level_dir, "pc_standardized.npy")
    regression_u = _open_array(level_dir, "regression_u.npy")
    regression_v = _open_array(level_dir, "regression_v.npy")
    regression_eta = _open_array(level_dir, "regression_eta.npy")
    time_utc_ns = _open_array(level_dir, "time_utc_ns.npy")
    explained_variance = _open_array(level_dir, "explained_variance.npy")
    valid_fraction = _open_array(level_dir, "cell_triplet_valid_fraction.npy")

    for name, array in (
        ("mean_u.npy", mean_u),
        ("mean_v.npy", mean_v),
        ("mean_eta.npy", mean_eta),
        ("cell_triplet_valid_fraction.npy", valid_fraction),
    ):
        _require(array.shape == spatial_shape, f"{level_dir.name}/{name} 必須是 (lat, lon)")
    _require(
        time_utc_ns.dtype == np.int64
        and time_utc_ns.ndim == 1
        and time_utc_ns.size >= 2
        and np.all(np.diff(time_utc_ns) > 0),
        f"{level_dir.name}/time_utc_ns.npy 必須是嚴格遞增的 int64 一維 UTC 軸",
    )
    _require(
        pc.ndim == 2 and pc.shape == pc_standardized.shape,
        f"{level_dir.name}/pc.npy 與 pc_standardized.npy 必須同為 (mode, time)",
    )
    _require(
        pc.shape[1] == time_utc_ns.size,
        f"{level_dir.name}/PC 時間維度必須對齊 time_utc_ns.npy",
    )
    mode_count = pc.shape[0]
    expected_mode_shape = (mode_count, *spatial_shape)
    for name, array in (
        ("regression_u.npy", regression_u),
        ("regression_v.npy", regression_v),
        ("regression_eta.npy", regression_eta),
    ):
        _require(
            array.shape == expected_mode_shape,
            f"{level_dir.name}/{name} 必須是 (mode, lat, lon)",
        )
    _require(
        explained_variance.shape == (mode_count,)
        and np.all(np.isfinite(explained_variance))
        and np.all(explained_variance >= 0.0),
        f"{level_dir.name}/explained_variance.npy 必須是有限非負的一維模態比例",
    )
    _require(
        np.all(np.isfinite(pc)) and np.all(np.isfinite(pc_standardized)),
        f"{level_dir.name}/PC 不可含 NaN 或無限值",
    )
    _require(
        np.all(np.isfinite(valid_fraction))
        and np.all((valid_fraction >= 0.0) & (valid_fraction <= 1.0)),
        f"{level_dir.name}/cell_triplet_valid_fraction.npy 必須介於 0 與 1",
    )

    pc_standard_deviation = np.std(pc, axis=1, ddof=1, dtype=np.float64)
    _require(
        np.all(np.isfinite(pc_standard_deviation))
        and np.all(pc_standard_deviation > 0.0),
        f"{level_dir.name}/raw PC 樣本標準差必須為有限正值",
    )
    pc_mean_max_abs = float(np.max(np.abs(np.mean(pc, axis=1, dtype=np.float64))))
    return FixedDepthLevelFigureInputs(
        mean_u=mean_u,
        mean_v=mean_v,
        mean_eta=mean_eta,
        visualization=AcademicVisualizationFields(
            pc_standardized=pc_standardized,
            pc_standard_deviation=pc_standard_deviation,
            regression_u=regression_u,
            regression_v=regression_v,
            regression_eta=regression_eta,
            pc_mean_max_abs=pc_mean_max_abs,
        ),
        time_utc_ns=time_utc_ns,
        explained_variance=explained_variance,
        cell_triplet_valid_fraction=valid_fraction,
    )


def _coverage_renderer_code_sha256() -> str:
    """雜湊共用地圖 renderer 與本模組，建立完整圖面程式 provenance。"""

    module_path_raw = inspect.getsourcefile(_make_fixed_depth_coverage_qc_figure)
    _require(isinstance(module_path_raw, str), "無法定位固定深度 coverage renderer 原始碼")
    return _canonical_json_hash(
        {
            "surface_renderer_sha256": _renderer_code_sha256(),
            "fixed_depth_renderer_module_sha256": _sha256_file(Path(module_path_raw)),
        }
    )


def _make_fixed_depth_coverage_qc_figure(
    *,
    output_dir: Path,
    lon: np.ndarray,
    lat: np.ndarray,
    analysis_geometry_mask: np.ndarray,
    shared_valid_mask: np.ndarray,
    level_ids: tuple[str, ...],
    level_labels_zh: tuple[str, ...],
    level_valid_fractions: tuple[np.ndarray, ...],
    config: AnalysisConfig,
) -> tuple[list[str], dict[str, Any]]:
    """建立四層年度 coverage 門檻與共同交集的 2×2 QC 地圖。

    藍色格代表該層三變數年度有效率達 `minimum_cell_valid_fraction`；橙色格仍位於原始
    analysis geometry，但該層未達門檻。陸地只由版本化 OSM polygon 疊加作地理參照，
    不參與格點分類。這項分色刻意不把沿岸失敗格畫成陸地，讓讀者可辨識它們是因固定
    深度水柱支撐不足而被共同分析排除。
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as MatplotlibPath
    from matplotlib.patches import Patch, PathPatch
    from matplotlib.ticker import FormatStrFormatter

    _require(len(level_ids) == 4, "目前 coverage/QC 正式版面要求表層、-5、-10、-20 m 共四層")
    _require(
        len(level_labels_zh) == len(level_ids)
        and len(level_valid_fractions) == len(level_ids),
        "coverage/QC 層位 ID、中文標籤與有效率陣列數量必須一致",
    )
    spatial_shape = (lat.size, lon.size)
    _require(
        analysis_geometry_mask.shape == spatial_shape
        and shared_valid_mask.shape == spatial_shape,
        "coverage/QC geometry 與 shared mask 必須對齊 (lat, lon)",
    )
    _require(
        np.all(shared_valid_mask <= analysis_geometry_mask),
        "shared_valid_mask 不可超出 analysis_geometry_mask",
    )

    lon_step = float(np.median(np.diff(lon)))
    lat_step = float(np.median(np.diff(lat)))
    geometry_rows, geometry_cols = np.where(analysis_geometry_mask)
    _require(geometry_rows.size > 0, "coverage/QC 至少需要一個 analysis geometry 格")
    lon_min = max(config.bbox[0], float(lon[geometry_cols].min()) - lon_step / 2.0)
    lon_max = min(config.bbox[1], float(lon[geometry_cols].max()) + lon_step / 2.0)
    lat_min = max(config.bbox[2], float(lat[geometry_rows].min()) - lat_step / 2.0)
    lat_max = min(config.bbox[3], float(lat[geometry_rows].max()) + lat_step / 2.0)
    plot_extent = (lon_min, lon_max, lat_min, lat_max)
    lon_grid, lat_grid = np.meshgrid(lon, lat)

    source_land_polygons = _load_geojson_land_polygons(config.figure_land_overlay_path)
    plot_land_polygons = _clip_land_polygons_to_extent(source_land_polygons, plot_extent)
    report_font_name, report_font_sha256 = _resolve_report_font()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [report_font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )

    def add_land_overlay(axis: Any) -> None:
        """在每個分面上疊相同 OSM 陸地，維持 coverage 分類的海陸地理語意。"""

        for polygon in plot_land_polygons:
            vertices: list[np.ndarray] = []
            codes: list[np.ndarray] = []
            for ring in polygon:
                ring_codes = np.full(ring.shape[0], MatplotlibPath.LINETO, dtype=np.uint8)
                ring_codes[0] = MatplotlibPath.MOVETO
                ring_codes[-1] = MatplotlibPath.CLOSEPOLY
                vertices.append(ring)
                codes.append(ring_codes)
            axis.add_patch(
                PathPatch(
                    MatplotlibPath(np.vstack(vertices), np.concatenate(codes)),
                    facecolor="#D9D6CF",
                    edgecolor="#4A4A4A",
                    linewidth=0.65,
                    joinstyle="round",
                    capstyle="round",
                    zorder=4,
                    clip_on=True,
                )
            )

    # 類別 0 是未達門檻、1 是達標；bbox 內但不屬 analysis geometry 的格保持 NaN。
    category_cmap = ListedColormap(["#D89032", "#2A6F97"])
    category_norm = BoundaryNorm([-0.5, 0.5, 1.5], category_cmap.N)
    geometry_count = int(np.count_nonzero(analysis_geometry_mask))
    shared_count = int(np.count_nonzero(shared_valid_mask))
    threshold = config.minimum_cell_valid_fraction
    panel_metadata: list[dict[str, Any]] = []

    fig, axes = plt.subplots(2, 2, figsize=(10.5, 10.0), sharex=True, sharey=True)
    for axis, level_id, label, valid_fraction in zip(
        axes.flat,
        level_ids,
        level_labels_zh,
        level_valid_fractions,
        strict=True,
    ):
        _require(valid_fraction.shape == spatial_shape, f"{level_id} coverage shape 必須對齊格網")
        pass_mask = analysis_geometry_mask & (valid_fraction >= threshold)
        category = np.full(spatial_shape, np.nan, dtype=np.float64)
        category[analysis_geometry_mask & ~pass_mask] = 0.0
        category[pass_mask] = 1.0
        axis.pcolormesh(
            lon_grid,
            lat_grid,
            np.ma.masked_invalid(category),
            shading="auto",
            cmap=category_cmap,
            norm=category_norm,
            rasterized=True,
            zorder=1,
        )
        add_land_overlay(axis)
        pass_count = int(np.count_nonzero(pass_mask))
        fail_count = geometry_count - pass_count
        axis.set_title(
            f"{label}：達標 {pass_count}/{geometry_count} 格",
            fontsize=12,
            pad=10,
        )
        axis.set_xlim(lon_min, lon_max)
        axis.set_ylim(lat_min, lat_max)
        axis.set_aspect(
            1.0 / np.cos(np.deg2rad((lat_min + lat_max) / 2.0)),
            adjustable="box",
        )
        axis.set_xticks(np.linspace(lon_min, lon_max, 4))
        axis.set_yticks(np.linspace(lat_min, lat_max, 4))
        axis.xaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.yaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.tick_params(labelsize=8)
        axis.grid(color="white", linewidth=0.45, alpha=0.38)
        panel_metadata.append(
            {
                "level_id": level_id,
                "label_zh": label,
                "threshold_pass_cell_count": pass_count,
                "threshold_fail_cell_count": fail_count,
                "analysis_geometry_cell_count": geometry_count,
            }
        )

    for axis in axes[:, 0]:
        axis.set_ylabel("緯度（°N）", fontsize=10)
    for axis in axes[-1, :]:
        axis.set_xlabel("經度（°E）", fontsize=10)
    fig.suptitle(
        f"{config.focus_name_zh}：固定深度四層 coverage QC（年度有效率門檻 ≥ {threshold:.0%}）",
        fontsize=16,
        y=0.985,
    )
    excluded_count = geometry_count - shared_count
    fig.text(
        0.5,
        0.925,
        f"四層共同交集：{shared_count}/{geometry_count} 格；共同排除 {excluded_count} 格",
        ha="center",
        va="center",
        fontsize=11,
    )
    fig.legend(
        handles=[
            Patch(facecolor="#2A6F97", edgecolor="none", label="該層年度有效率達標"),
            Patch(facecolor="#D89032", edgecolor="none", label="該層年度有效率未達門檻"),
            Patch(facecolor="#D9D6CF", edgecolor="#4A4A4A", label="OSM 陸地"),
            Line2D([], [], color="none", label="共同遮罩採四層交集，不補值、不外插"),
        ],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.012),
        ncol=2,
        frameon=True,
        facecolor="white",
        framealpha=0.96,
        fontsize=9,
    )
    fig.subplots_adjust(left=0.09, right=0.98, bottom=0.11, top=0.89, wspace=0.12, hspace=0.18)
    fig.patch.set_facecolor("white")
    fig.patch.set_alpha(1.0)

    report_dir = output_dir / "figures" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    stem = "fixed_depth_shared_coverage_qc_report"
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
        fig.savefig(path, **save_kwargs)
        created.append(str(path.relative_to(output_dir)))
    plt.close(fig)

    metadata = {
        "schema_name": "ocm_fixed_depth_shared_coverage_qc_figure",
        "schema_version": "1.0.0",
        "minimum_cell_triplet_valid_fraction": threshold,
        "analysis_geometry_cell_count": geometry_count,
        "shared_common_cell_count": shared_count,
        "shared_excluded_cell_count": excluded_count,
        "comparison_mask_policy": "intersection_with_surface",
        "category_semantics": {
            "pass": "analysis geometry cell with level triplet valid fraction >= threshold",
            "fail": "analysis geometry cell with level triplet valid fraction < threshold",
            "land": "versioned OSM polygon overlay; visual reference only",
        },
        "panels": panel_metadata,
        "plot_extent_lon_lat": [lon_min, lon_max, lat_min, lat_max],
        "coastline": {
            "logical_path": config.figure_land_overlay_logical_path,
            "sha256": config.figure_land_overlay_sha256,
            "source_polygon_count": len(source_land_polygons),
            "plotted_polygon_count": len(plot_land_polygons),
        },
        "renderer": {
            "font_name": report_font_name,
            "font_file_sha256": report_font_sha256,
        },
        "files": created,
        "limitations": [
            "此圖只呈現年度有效率門檻與四層共同交集，不顯示流速、eta 或模式幅度。",
            "橙色格仍是 analysis geometry 內的海洋候選格，不可解讀為陸地。",
            "共同排除格不做補值、水平外插或垂向外插。",
        ],
    }
    _write_json(output_dir / "figures" / "coverage_qc_metadata.json", metadata)
    return created, metadata


def _write_bundle_guide(
    *,
    bundle_dir: Path,
    source_run_id: str,
    level_summaries: list[dict[str, Any]],
    coverage_metadata: dict[str, Any],
) -> None:
    """寫出固定深度圖包的最小選圖與 coverage 解讀指南。"""

    lines = [
        "# 固定深度 SVD 正式圖包指南",
        "",
        f"- 來源 family run：`{source_run_id}`",
        "- 本圖包只讀既有陣列重繪；未讀 native/surface cache，未重新內插或求解 SVD。",
        (
            "- 四層共同 coverage："
            f"{coverage_metadata['shared_common_cell_count']}/"
            f"{coverage_metadata['analysis_geometry_cell_count']} 格；"
            f"共同排除 {coverage_metadata['shared_excluded_cell_count']} 格。"
        ),
        "",
        "## 選圖規則",
        "",
        "1. 直接放入報告時，使用同 stem 的 `*_with_vector_scale.png` 或 SVG。",
        "2. 需要自行排版時，使用標準主圖加 `*_vector_scale_transparent.svg`，不可跨模態共用比例尺。",
        "3. `fixed_depth_shared_coverage_qc_report` 必須與垂向比較結果一起保存，用來交代共同遮罩來源。",
        "4. coverage 圖的橙色格是未達該層年度門檻的海洋候選格，不是陸地，也不得回填。",
        "",
        "## 層位",
        "",
    ]
    for level in level_summaries:
        lines.append(
            f"- `{level['level_id']}`：{level['velocity_context_zh']}；"
            f"圖目錄 `{level['relative_path']}/figures/report/`"
        )
    (bundle_dir / "REPORT_GUIDE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def replot_fixed_depth_multivariate_svd(
    *,
    run_dir: Path,
    output_root: Path,
    config_path: Path | None = None,
) -> Path:
    """唯讀既有固定深度 family，原子發布最新正式圖包。

    `config_path` 省略時沿用來源 `config.json`；若提供新設定，只允許 `figures` 區段
    不同。輸出放在
    `fixed_depth_svd_figure_bundles/<analysis_label_vN>/<style>/`，與完整表層的
    `svd_figure_bundles/` 分開。來源科學 family 不內嵌或修改圖面，讓新版比例尺與 QC
    能獨立升版，又不破壞全年運算的 provenance。
    """

    performance = PerformanceRecorder()
    with performance.measure("source_and_configuration_validation"):
        resolved_run_dir = run_dir.resolve()
        resolved_output_root = output_root.resolve()
        _require(resolved_run_dir.is_dir(), f"固定深度 family 目錄不存在: {resolved_run_dir}")
        source_metadata_path = resolved_run_dir / "metadata.json"
        source_config_path = resolved_run_dir / "config.json"
        source_metadata = _read_json_object(source_metadata_path)
        source_config_raw = _read_json_object(source_config_path)
        source_run_id = source_metadata.get("run_id")
        _require(
            isinstance(source_run_id, str) and source_run_id == resolved_run_dir.name,
            "固定深度來源 metadata.run_id 必須與 run 目錄名稱一致",
        )
        _require(
            source_metadata.get("schema_name") == "ocm_fixed_depth_multivariate_svd_family",
            "重繪來源必須是正式固定深度 multivariate SVD family",
        )
        source_science_provenance_sha256 = source_metadata.get(
            "science_provenance_sha256"
        )
        _require(
            isinstance(source_science_provenance_sha256, str)
            and len(source_science_provenance_sha256) == 64
            and all(
                character in "0123456789abcdef"
                for character in source_science_provenance_sha256
            ),
            "固定深度來源 metadata 必須保存完整 science_provenance_sha256；"
            "可讀版本目錄不可取代內容追溯。",
        )
        render_config_path = config_path.resolve() if config_path is not None else source_config_path
        fixed_config = load_fixed_depth_config(render_config_path)
        render_config = fixed_config.base
        _require(
            render_config.raw.get("analysis_kind") == FIXED_DEPTH_ANALYSIS_KIND
            and render_config.raw.get("svd", {}).get("variables") == list(FIXED_DEPTH_VARIABLES),
            "固定深度重繪設定的 analysis kind 與三變數契約不正確",
        )
        source_science_hash = _science_config_hash(source_config_raw)
        _require(
            _science_config_hash(render_config.raw) == source_science_hash,
            "固定深度重繪設定除 figures 外必須與來源 run 完全相同；科學設定變更必須建立新 run",
        )
        source_metadata_sha256 = _sha256_file(source_metadata_path)
        renderer_code_sha256 = _coverage_renderer_code_sha256()
        renderer_environment = _renderer_environment_signature()
        bundle_provenance_sha256 = _canonical_json_hash(
            {
                "source_run_id": source_run_id,
                "source_science_provenance_sha256": (
                    source_science_provenance_sha256
                ),
                "source_metadata_sha256": source_metadata_sha256,
                "source_science_config_sha256": source_science_hash,
                "figure_config": render_config.raw["figures"],
                "renderer_code_sha256": renderer_code_sha256,
                "renderer_environment": renderer_environment,
            }
        )
        safe_style = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in render_config.figure_style
        )
        bundle_id = safe_style
        final_dir = (
            resolved_output_root
            / "fixed_depth_svd_figure_bundles"
            / source_run_id
            / bundle_id
        )
        if final_dir.exists():
            raise FileExistsError(
                "固定深度 figure style 版本已發布，拒絕覆寫；若圖面規格改變，"
                f"請提升 figures.style: {final_dir}"
            )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        partial_dir = final_dir.parent / f".{bundle_id}.partial-{uuid.uuid4().hex}"
        partial_dir.mkdir(parents=False)

    try:
        with performance.measure("existing_family_array_loading"):
            lon = _open_array(resolved_run_dir, "lon.npy")
            lat = _open_array(resolved_run_dir, "lat.npy")
            analysis_geometry_mask = _open_array(
                resolved_run_dir,
                "analysis_geometry_mask.npy",
            )
            shared_valid_mask = _open_array(
                resolved_run_dir,
                "shared_valid_mask.npy",
            )
            _require(
                lon.ndim == 1
                and lat.ndim == 1
                and lon.size >= 2
                and lat.size >= 2
                and np.all(np.diff(lon) > 0)
                and np.all(np.diff(lat) > 0),
                "固定深度 family 的 lon/lat 必須是嚴格遞增一維軸",
            )
            spatial_shape = (lat.size, lon.size)
            _require(
                analysis_geometry_mask.dtype == np.bool_
                and shared_valid_mask.dtype == np.bool_
                and analysis_geometry_mask.shape == spatial_shape
                and shared_valid_mask.shape == spatial_shape,
                "固定深度 geometry/shared mask 必須是對齊格網的 bool 陣列",
            )

            raw_levels = source_metadata.get("levels")
            _require(isinstance(raw_levels, list) and len(raw_levels) == 4, "固定深度 family 必須列出四個層位")
            level_records: list[dict[str, Any]] = []
            for raw_level in raw_levels:
                _require(isinstance(raw_level, dict), "固定深度 metadata.levels 每項必須是物件")
                level_id = raw_level.get("level_id")
                relative_path = raw_level.get("relative_path")
                _require(
                    isinstance(level_id, str) and isinstance(relative_path, str),
                    "固定深度 level 必須具有 level_id 與 relative_path",
                )
                level_dir = (resolved_run_dir / relative_path).resolve()
                _require(
                    level_dir.parent == resolved_run_dir / "levels"
                    and level_dir.is_dir(),
                    f"固定深度 level 路徑不可超出來源 family: {relative_path}",
                )
                level_metadata = _read_json_object(level_dir / "metadata.json")
                _require(
                    level_metadata.get("family_run_id") == source_run_id
                    and level_metadata.get("level_id") == level_id,
                    f"{level_id} metadata 必須回指同一 family run",
                )
                level_records.append(
                    {
                        "level_id": level_id,
                        "relative_path": relative_path,
                        "target_depth_m_below_vertical_datum": raw_level.get(
                            "target_depth_m_below_vertical_datum"
                        ),
                        "velocity_context_zh": level_metadata.get(
                            "velocity_context_zh"
                        ),
                        "inputs": _load_level_inputs(
                            level_dir,
                            spatial_shape=spatial_shape,
                        ),
                    }
                )

        with performance.measure("level_figure_rendering"):
            all_figure_files: list[str] = []
            level_summaries: list[dict[str, Any]] = []
            for record in level_records:
                level_id = record["level_id"]
                target_depth = record["target_depth_m_below_vertical_datum"]
                velocity_context = record["velocity_context_zh"]
                _require(
                    isinstance(velocity_context, str) and velocity_context.strip(),
                    f"{level_id} metadata 缺少 velocity_context_zh",
                )
                level_bundle_dir = partial_dir / "levels" / level_id
                level_bundle_dir.mkdir(parents=True)
                inputs: FixedDepthLevelFigureInputs = record["inputs"]
                level_files = _make_figures(
                    level_bundle_dir,
                    lon,
                    lat,
                    inputs.mean_u,
                    inputs.mean_v,
                    inputs.mean_eta,
                    inputs.visualization,
                    inputs.time_utc_ns,
                    inputs.explained_variance,
                    render_config,
                    velocity_context_zh=velocity_context,
                    mean_asset_stem=(
                        "mean_surface_reference_flow_report"
                        if target_depth is None
                        else "mean_fixed_depth_flow_report"
                    ),
                    mean_asset_key=(
                        "mean_surface_reference_flow"
                        if target_depth is None
                        else "mean_fixed_depth_flow"
                    ),
                )
                prefixed_files = [
                    str(Path("levels") / level_id / relative_path)
                    for relative_path in level_files
                ]
                all_figure_files.extend(prefixed_files)
                level_summaries.append(
                    {
                        "level_id": level_id,
                        "target_depth_m_below_vertical_datum": target_depth,
                        "velocity_context_zh": velocity_context,
                        "relative_path": str(Path("levels") / level_id),
                        "figures": prefixed_files,
                    }
                )

        with performance.measure("shared_coverage_qc_rendering"):
            coverage_files, coverage_metadata = _make_fixed_depth_coverage_qc_figure(
                output_dir=partial_dir,
                lon=lon,
                lat=lat,
                analysis_geometry_mask=analysis_geometry_mask,
                shared_valid_mask=shared_valid_mask,
                level_ids=tuple(record["level_id"] for record in level_records),
                level_labels_zh=tuple(
                    record["velocity_context_zh"] for record in level_records
                ),
                level_valid_fractions=tuple(
                    record["inputs"].cell_triplet_valid_fraction
                    for record in level_records
                ),
                config=render_config,
            )
            all_figure_files.extend(coverage_files)

        with performance.measure("bundle_provenance_serialization"):
            _require(
                _sha256_file(source_metadata_path) == source_metadata_sha256,
                "重繪期間來源 family metadata 已改變，拒絕發布圖包",
            )
            _write_json(partial_dir / "figure_config.json", render_config.raw["figures"])
            _write_bundle_guide(
                bundle_dir=partial_dir,
                source_run_id=source_run_id,
                level_summaries=level_summaries,
                coverage_metadata=coverage_metadata,
            )
            bundle_metadata = {
                "schema_name": "ocm_fixed_depth_svd_figure_bundle",
                "schema_version": FIXED_DEPTH_FIGURE_BUNDLE_SCHEMA_VERSION,
                "status": "figures_ready",
                "bundle_id": bundle_id,
                "bundle_provenance_sha256": bundle_provenance_sha256,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "source_run": {
                    "run_id": source_run_id,
                    "status": source_metadata.get("status"),
                    "science_provenance_sha256": (
                        source_science_provenance_sha256
                    ),
                    "metadata_sha256": source_metadata_sha256,
                    "science_config_sha256_excluding_figures": source_science_hash,
                    "read_policy": "read-only NumPy memory-map; source arrays are referenced, not copied",
                    "native_cache_read": False,
                    "surface_cache_read": False,
                    "vertical_interpolation_called": False,
                    "svd_solver_called": False,
                },
                "renderer": {
                    "style": render_config.figure_style,
                    "level_renderer_function": (
                        "ocm_svd_analysis.surface_multivariate_svd._make_figures"
                    ),
                    "coverage_renderer_function": (
                        "ocm_svd_analysis.fixed_depth_replot."
                        "_make_fixed_depth_coverage_qc_figure"
                    ),
                    "renderer_code_sha256": renderer_code_sha256,
                    "environment": renderer_environment,
                },
                "figure_config": render_config.raw["figures"],
                "levels": level_summaries,
                "coverage_qc": coverage_metadata,
                "figures": all_figure_files,
                "limitations": [
                    "本圖包只重繪既有固定深度科學陣列，沒有讀取 paired cache、重新垂向內插或重新求解 SVD。",
                    "coverage 橙色格是 analysis geometry 內未達該層年度有效率門檻的海洋候選格，不是陸地。",
                    (
                        "四層空間圖仍使用來源 run 的共同 "
                        f"{coverage_metadata['shared_common_cell_count']} 格遮罩；"
                        "沒有填補共同排除格。"
                    ),
                ],
            }
        bundle_metadata["performance"] = performance.to_metadata(
            scope_end=(
                "從固定深度重繪函式入口至 bundle provenance 組裝；不含最後"
                " metadata.json 寫入與原子目錄 rename。"
            )
        )
        _write_json(partial_dir / "metadata.json", bundle_metadata)
        os.replace(partial_dir, final_dir)
    except Exception:
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
    return final_dir
