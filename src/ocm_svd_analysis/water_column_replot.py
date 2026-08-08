"""從既有六層聯合水柱 SVD 成果重繪獨立圖面，不回讀快取或重新求解。

水柱直接 SVD 的完整 2024–2025 求解會讀取 paired ``ocm_surface``／``ocm_native``、建立大型
磁碟矩陣並以 PROPACK 取得設定模態數；這是昂貴且不應為圖面修正重複執行的科學步驟。
本模組只以唯讀 memory-map 開啟已發布 run 的回歸場、PC、遮罩與規則座標，將新版獨立圖面
發布為另一個 immutable figure bundle。來源 run 的科學陣列、metadata、舊圖與上游 cache
均不會被寫入、複製或重新計算。
"""

from __future__ import annotations

import hashlib
import inspect
import os
import platform
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .performance import PerformanceRecorder
from .surface_multivariate_svd import (
    _canonical_json_hash,
    _read_json_object,
    _require,
    _resolve_report_font,
    _write_json,
)
from .water_column_multivariate_svd import (
    VELOCITY_LEVEL_IDS,
    WATER_COLUMN_ANALYSIS_KIND,
    WATER_COLUMN_FIGURE_STYLE,
    WaterColumnConfig,
    WaterColumnFigureGrid,
    _make_water_column_figures,
    load_water_column_config,
)


WATER_COLUMN_FIGURE_BUNDLE_SCHEMA_VERSION = "1.0.0"
"""只讀水柱重繪 bundle 的 metadata schema；與科學 run schema 分開版本化。"""

WATER_COLUMN_REPLOT_REQUIRED_ARRAY_FILENAMES = (
    "lon.npy",
    "lat.npy",
    "time_utc_ns.npy",
    "regression_u_mps_per_pc_std.npy",
    "regression_v_mps_per_pc_std.npy",
    "regression_eta_m_per_pc_std.npy",
    "pc_standardized.npy",
    "explained_variance.npy",
    "velocity_feature_mask.npy",
    "eta_feature_mask.npy",
)
"""重繪的最小、已發布陣列集合；不含 native/surface cache 或任何 SVD 暫存矩陣。"""


@dataclass(frozen=True)
class ExistingWaterColumnFigureInputs:
    """從既有水柱 run 唯讀開啟、足以重繪的資料視圖。

    所有大型回歸場均維持 ``numpy.memmap``；圖面函式只讀取它們並生成輸出圖檔，不會回推
    raw PC、重新正規化、重新固定符號，或重建加權矩陣。``grid`` 僅含經緯度座標，刻意不
    搭載 native 內插欄位，以確保本流程不會依賴或讀取上游三維快取。
    """

    grid: WaterColumnFigureGrid
    time_utc_ns: np.ndarray
    regression_u: np.ndarray
    regression_v: np.ndarray
    regression_eta: np.ndarray
    pc_standardized: np.ndarray
    explained_variance: np.ndarray
    velocity_mask: np.ndarray
    eta_mask: np.ndarray


def _sha256_file(path: Path) -> str:
    """以固定區塊串流計算來源檔 SHA-256，避免複製大型回歸場到 RAM。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _science_config_hash(raw: dict[str, Any]) -> str:
    """雜湊排除 ``figures`` 後的設定，限制 replot 只能調整視覺交付層。

    新圖面可變更輸出格式、DPI、岸線或向量密度；但年份、完整 flow-domain bbox、缺值
    策略、深度、權重與 SVD 設定必須和來源 run 完全相同。此檢查避免為既有陣列套上另一
    份科學設定的地名、時間範圍或單位，卻誤稱為純重繪。
    """

    return _canonical_json_hash({key: value for key, value in raw.items() if key != "figures"})


def _open_run_array(run_dir: Path, filename: str) -> np.ndarray:
    """以唯讀 memory-map 開啟既有 NPY，拒絕遺失檔與 pickle 序列化。"""

    path = run_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"既有水柱 SVD run 缺少重繪必要陣列: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _load_existing_water_column_figure_inputs(
    run_dir: Path,
    *,
    config: WaterColumnConfig,
    source_metadata: dict[str, Any],
) -> ExistingWaterColumnFigureInputs:
    """交叉驗證既有水柱成果的軸、模態、遮罩與回歸圖資料結構。

    檢查只確認已發布陣列的維度、時間對齊與有限值，資料值不會重新估計。六層速度陣列
    必須維持 ``(mode, velocity_level, lat, lon)``，eta 必須維持無 depth 軸的
    ``(mode, lat, lon)``；若來源缺少其中任一項，流程停止而不以舊圖、raw PC 或假設值補造。
    """

    lon = _open_run_array(run_dir, "lon.npy")
    lat = _open_run_array(run_dir, "lat.npy")
    time_utc_ns = _open_run_array(run_dir, "time_utc_ns.npy")
    regression_u = _open_run_array(run_dir, "regression_u_mps_per_pc_std.npy")
    regression_v = _open_run_array(run_dir, "regression_v_mps_per_pc_std.npy")
    regression_eta = _open_run_array(run_dir, "regression_eta_m_per_pc_std.npy")
    pc_standardized = _open_run_array(run_dir, "pc_standardized.npy")
    explained_variance = _open_run_array(run_dir, "explained_variance.npy")
    velocity_mask = _open_run_array(run_dir, "velocity_feature_mask.npy")
    eta_mask = _open_run_array(run_dir, "eta_feature_mask.npy")

    _require(
        lon.ndim == 1 and lon.size >= 2 and np.all(np.isfinite(lon)) and np.all(np.diff(lon) > 0.0),
        "重繪來源 lon.npy 必須是有限且嚴格遞增的一維經度軸",
    )
    _require(
        lat.ndim == 1 and lat.size >= 2 and np.all(np.isfinite(lat)) and np.all(np.diff(lat) > 0.0),
        "重繪來源 lat.npy 必須是有限且嚴格遞增的一維緯度軸",
    )
    spatial_shape = (lat.size, lon.size)
    _require(
        time_utc_ns.dtype == np.int64 and time_utc_ns.ndim == 1 and time_utc_ns.size >= 2,
        "重繪來源 time_utc_ns.npy 必須是至少兩筆 int64 UTC 軸",
    )
    _require(np.all(np.diff(time_utc_ns) > 0), "重繪來源 time_utc_ns.npy 必須嚴格遞增")
    _require(
        pc_standardized.ndim == 2 and pc_standardized.shape[1] == time_utc_ns.size,
        "pc_standardized.npy 必須是 (mode, time) 且與既有 UTC 軸對齊",
    )
    mode_count = int(pc_standardized.shape[0])
    _require(mode_count >= config.figure_mode_count, "既有水柱 run 的模態數不足以產生設定指定圖面")
    _require(
        regression_u.shape == (mode_count, len(VELOCITY_LEVEL_IDS), *spatial_shape),
        "regression_u_mps_per_pc_std.npy 必須是 (mode, velocity_level, lat, lon)",
    )
    _require(
        regression_v.shape == regression_u.shape,
        "regression_v_mps_per_pc_std.npy 必須與 regression_u 維度一致",
    )
    _require(
        regression_eta.shape == (mode_count, *spatial_shape),
        "regression_eta_m_per_pc_std.npy 必須是 (mode, lat, lon)，eta 不得具有 depth 軸",
    )
    _require(
        velocity_mask.shape == (len(VELOCITY_LEVEL_IDS), *spatial_shape),
        "velocity_feature_mask.npy 必須是 (velocity_level, lat, lon)",
    )
    _require(eta_mask.shape == spatial_shape, "eta_feature_mask.npy 必須是 (lat, lon)")
    _require(
        explained_variance.ndim == 1 and explained_variance.size == mode_count,
        "explained_variance.npy 必須與既有 PC 模態數一致",
    )
    _require(np.all(np.isfinite(pc_standardized)), "既有標準化 PC 不可含 NaN 或無限值")
    _require(
        np.all(np.isfinite(explained_variance)) and np.all(explained_variance >= 0.0),
        "既有 explained variance 必須是有限非負值",
    )
    _require(np.all(np.isfinite(regression_u[:, velocity_mask])), "有效速度 feature 的 u 回歸場不可含 NaN")
    _require(np.all(np.isfinite(regression_v[:, velocity_mask])), "有效速度 feature 的 v 回歸場不可含 NaN")
    _require(np.all(np.isfinite(regression_eta[:, eta_mask])), "有效 eta feature 的回歸場不可含 NaN")

    metadata_svd = source_metadata.get("svd")
    _require(isinstance(metadata_svd, dict), "來源 metadata 缺少 svd 區段")
    _require(int(metadata_svd.get("mode_count", -1)) == mode_count, "來源 metadata.svd.mode_count 與陣列不一致")
    return ExistingWaterColumnFigureInputs(
        grid=WaterColumnFigureGrid(lon=lon, lat=lat),
        time_utc_ns=time_utc_ns,
        regression_u=regression_u,
        regression_v=regression_v,
        regression_eta=regression_eta,
        pc_standardized=pc_standardized,
        explained_variance=explained_variance,
        velocity_mask=np.asarray(velocity_mask, dtype=bool),
        eta_mask=np.asarray(eta_mask, dtype=bool),
    )


def _renderer_code_sha256() -> str:
    """雜湊水柱 renderer 所在模組，讓 helper 修改也會反映在 bundle provenance。"""

    source_path = inspect.getsourcefile(_make_water_column_figures)
    _require(isinstance(source_path, str), "無法定位水柱獨立圖面 renderer 原始碼")
    return _sha256_file(Path(source_path))


def _renderer_environment_signature() -> dict[str, str]:
    """記錄會影響 PNG/SVG 位元內容的 Python、Matplotlib、FreeType 與中文字型版本。"""

    import matplotlib
    import matplotlib.ft2font

    font_name, font_sha256 = _resolve_report_font()
    return {
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "matplotlib_version": matplotlib.__version__,
        "freetype_version": matplotlib.ft2font.__freetype_version__,
        "report_font_name": font_name,
        "report_font_file_sha256": font_sha256,
    }


def replot_water_column_multivariate_svd(
    *,
    run_dir: Path,
    output_root: Path,
    config_path: Path | None = None,
) -> Path:
    """只讀既有水柱 SVD 陣列，原子發布新版獨立 figure bundle。

    ``run_dir`` 必須是已發布的 ``water_column_svd/<analysis_label>/``。若未指定
    ``config_path``，重繪沿用來源保存的設定；若指定新版設定，只允許 ``figures`` 區段不同，
    以便加入 SVG、岸線與新版圖面資產，而不能改變水柱科學定義。輸出固定在
    ``water_column_svd_figure_bundles/<analysis_label>/<figure-style>/``，與來源科學 run
    嚴格分離。整個過程不接受 native/surface root 參數，也不匯入或呼叫任何 SVD 求解函式。
    """

    performance = PerformanceRecorder()
    with performance.measure("source_and_configuration_validation"):
        resolved_run_dir = run_dir.resolve()
        resolved_output_root = output_root.resolve()
        _require(resolved_run_dir.is_dir(), f"既有水柱 SVD run 目錄不存在: {resolved_run_dir}")
        _require(
            not resolved_output_root.is_relative_to(resolved_run_dir),
            "water-column figure bundle output_root 不可位於來源 immutable run 內",
        )
        source_metadata_path = resolved_run_dir / "metadata.json"
        source_config_path = resolved_run_dir / "config.json"
        source_metadata = _read_json_object(source_metadata_path)
        source_config_raw = _read_json_object(source_config_path)
        source_run_id = source_metadata.get("analysis_label")
        _require(
            isinstance(source_run_id, str) and source_run_id == resolved_run_dir.name,
            "來源 metadata.analysis_label 必須與 water_column_svd run 目錄名稱一致",
        )
        _require(
            source_metadata.get("schema_name") == "ocm_water_column_multivariate_svd"
            and source_metadata.get("analysis_kind") == WATER_COLUMN_ANALYSIS_KIND,
            "重繪來源必須是已發布的 water_column_multivariate_svd run",
        )
        render_config_path = config_path.resolve() if config_path is not None else source_config_path
        render_config = load_water_column_config(render_config_path)
        source_science_hash = _science_config_hash(source_config_raw)
        _require(
            _science_config_hash(render_config.raw) == source_science_hash,
            "水柱重繪設定除 figures 外必須與來源 run/config.json 完全相同",
        )
        source_hash_paths = (source_metadata_path, source_config_path) + tuple(
            resolved_run_dir / filename for filename in WATER_COLUMN_REPLOT_REQUIRED_ARRAY_FILENAMES
        )
        source_file_hashes_before = {
            str(path.relative_to(resolved_run_dir)): _sha256_file(path) for path in source_hash_paths
        }
        renderer_code_sha256 = _renderer_code_sha256()
        renderer_environment = _renderer_environment_signature()
        bundle_provenance_sha256 = _canonical_json_hash(
            {
                "source_run_id": source_run_id,
                "source_files_sha256": source_file_hashes_before,
                "source_science_config_sha256_excluding_figures": source_science_hash,
                "figure_config": render_config.raw["figures"],
                "renderer_code_sha256": renderer_code_sha256,
                "renderer_environment": renderer_environment,
            }
        )
        bundle_id = WATER_COLUMN_FIGURE_STYLE
        final_dir = (
            resolved_output_root
            / "water_column_svd_figure_bundles"
            / source_run_id
            / bundle_id
        )
        if final_dir.exists():
            raise FileExistsError(
                "此水柱圖面版本已發布，拒絕覆寫；若圖面規格確實改變，請先提升 "
                f"WATER_COLUMN_FIGURE_STYLE: {final_dir}"
            )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        partial_dir = final_dir.parent / f".{bundle_id}.partial-{uuid.uuid4().hex}"
        partial_dir.mkdir(parents=False)

    try:
        with performance.measure("existing_water_column_array_loading"):
            inputs = _load_existing_water_column_figure_inputs(
                resolved_run_dir,
                config=render_config,
                source_metadata=source_metadata,
            )
        with performance.measure("independent_figure_rendering"):
            figure_files, figure_info = _make_water_column_figures(
                partial_dir,
                config=render_config,
                grid=inputs.grid,
                time_utc_ns=inputs.time_utc_ns,
                regression_u=inputs.regression_u,
                regression_v=inputs.regression_v,
                regression_eta=inputs.regression_eta,
                pc_standardized=inputs.pc_standardized,
                explained_variance=inputs.explained_variance,
                velocity_mask=inputs.velocity_mask,
                eta_mask=inputs.eta_mask,
            )
        with performance.measure("bundle_provenance_serialization"):
            source_file_hashes_after = {
                str(path.relative_to(resolved_run_dir)): _sha256_file(path) for path in source_hash_paths
            }
            _require(
                source_file_hashes_after == source_file_hashes_before,
                "重繪期間來源水柱 SVD metadata、設定或陣列已改變，拒絕發布 figure bundle",
            )
            _write_json(partial_dir / "figure_config.json", render_config.raw["figures"])
            bundle_metadata = {
                "schema_name": "ocm_water_column_svd_figure_bundle",
                "schema_version": WATER_COLUMN_FIGURE_BUNDLE_SCHEMA_VERSION,
                "status": "figures_ready",
                "bundle_id": bundle_id,
                "bundle_provenance_sha256": bundle_provenance_sha256,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_run": {
                    "run_id": source_run_id,
                    "status": source_metadata.get("status"),
                    "metadata_sha256": source_file_hashes_before["metadata.json"],
                    "science_config_sha256_excluding_figures": source_science_hash,
                    "source_files_sha256": source_file_hashes_before,
                    "read_policy": "read-only NumPy memory-map; source arrays are referenced and never modified",
                    "surface_cache_read": False,
                    "native_cache_read": False,
                    "svd_solver_called": False,
                },
                "renderer": {
                    "style": WATER_COLUMN_FIGURE_STYLE,
                    "renderer_function": "ocm_svd_analysis.water_column_multivariate_svd._make_water_column_figures",
                    "renderer_code_sha256": renderer_code_sha256,
                    "environment": renderer_environment,
                },
                "figure_config": render_config.raw["figures"],
                "figures": {
                    "files": figure_files,
                    "renderer_info": figure_info,
                },
                "limitations": [
                    "本 bundle 只讀既有水柱 SVD 陣列，不讀取 ocm_surface、ocm_native 或任何 raw NetCDF。",
                    "本 bundle 不會重新建立加權矩陣、重新插補、重新正規化、重新固定模態符號或呼叫 SVD solver。",
                    "圖面科學意義與限制完全沿用 source_run；本 bundle 是衍生交付物，不取代既有科學成果。",
                ],
            }
        bundle_metadata["performance"] = performance.to_metadata(
            scope_end=(
                "從只讀水柱重繪入口至 bundle provenance 組裝；不含最後 metadata.json "
                "寫入與原子目錄 rename。"
            )
        )
        _write_json(partial_dir / "metadata.json", bundle_metadata)
        os.replace(partial_dir, final_dir)
    except Exception:
        # 只清除本次 UUID partial bundle；來源 immutable run、來源圖檔與既有 bundle 均不修改。
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
    return final_dir
