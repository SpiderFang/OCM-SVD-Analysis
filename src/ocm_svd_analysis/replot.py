"""從不可覆寫的既有 SVD run 建立獨立、可追溯的 figure bundle。

重繪流程只讀既有 run 內的平均場、回歸空間模態、標準化 PC、時間軸與 explained
variance，不讀上游 surface cache，也不呼叫 SVD 求解器。圖檔發布到獨立的
`svd_figure_bundles/` 樹，避免為了改 DPI、格式或繪圖程式而覆寫原科學成果。
"""

from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .performance import PerformanceRecorder
from .surface_multivariate_svd import (
    AcademicVisualizationFields,
    AnalysisConfig,
    _canonical_json_hash,
    _make_figures,
    _read_json_object,
    _require,
    _write_json,
    load_analysis_config,
)


FIGURE_BUNDLE_SCHEMA_VERSION = "1.0.0"
"""獨立 figure bundle metadata 的版本；與科學 run schema 分開演進。"""


@dataclass(frozen=True)
class ExistingRunFigureInputs:
    """從既有 run 以唯讀 memory-map 開啟的最小繪圖資料。

    陣列不會複製到 figure bundle；bundle 只保存圖檔、圖面 sidecar 與來源 run 雜湊。
    `visualization` 的回歸場與 PC 均直接取自既有科學成果，因此重繪不會重新估計模態、
    改變正負號或因 BLAS 差異得到另一組數值。
    """

    lon: np.ndarray
    lat: np.ndarray
    mean_u: np.ndarray
    mean_v: np.ndarray
    mean_eta: np.ndarray
    visualization: AcademicVisualizationFields
    time_utc_ns: np.ndarray
    explained_variance: np.ndarray


def _sha256_file(path: Path) -> str:
    """串流計算檔案 SHA-256，不一次把 metadata 或來源檔讀入大型記憶體。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _science_config_hash(raw: dict[str, Any]) -> str:
    """雜湊除 `figures` 外的全部分析設定，確保重繪只能改視覺參數。

    若 bbox、年份、缺值規則、正規化或模式數不同，候選設定就不再描述來源 run；流程必須
    停止，而不是拿錯的時間步長或地理範圍替既有陣列畫圖。
    """

    return _canonical_json_hash({key: value for key, value in raw.items() if key != "figures"})


def _open_run_array(run_dir: Path, filename: str) -> np.ndarray:
    """以唯讀 memory-map 開啟既有 `.npy`，拒絕遺失或含 pickle 的來源。"""

    path = run_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"既有 SVD run 缺少重繪必要陣列: {path}")
    return np.load(path, mmap_mode="r", allow_pickle=False)


def _load_existing_run_figure_inputs(run_dir: Path, metadata: dict[str, Any]) -> ExistingRunFigureInputs:
    """載入並交叉驗證既有 run 的圖面陣列 shape、時間軸與模態數。

    此處只做結構與有限值契約檢查，不重新執行 SVD 或回歸。若來源 run 是尚未包含
    `pc_standardized.npy`／`regression_*.npy` 的舊版，必須先以原版程式保留一次可追溯
    科學 run，不能由重繪器猜測缺少的分析定義。
    """

    lon = _open_run_array(run_dir, "lon.npy")
    lat = _open_run_array(run_dir, "lat.npy")
    mean_u = _open_run_array(run_dir, "mean_u.npy")
    mean_v = _open_run_array(run_dir, "mean_v.npy")
    mean_eta = _open_run_array(run_dir, "mean_eta.npy")
    pc_standardized = _open_run_array(run_dir, "pc_standardized.npy")
    regression_u = _open_run_array(run_dir, "regression_u.npy")
    regression_v = _open_run_array(run_dir, "regression_v.npy")
    regression_eta = _open_run_array(run_dir, "regression_eta.npy")
    time_utc_ns = _open_run_array(run_dir, "time_utc_ns.npy")
    explained_variance = _open_run_array(run_dir, "explained_variance.npy")

    _require(lon.ndim == 1 and lon.size >= 2 and np.all(np.isfinite(lon)) and np.all(np.diff(lon) > 0), "重繪來源 lon.npy 必須是有限、嚴格遞增的一維軸")
    _require(lat.ndim == 1 and lat.size >= 2 and np.all(np.isfinite(lat)) and np.all(np.diff(lat) > 0), "重繪來源 lat.npy 必須是有限、嚴格遞增的一維軸")
    spatial_shape = (lat.size, lon.size)
    for name, array in (("mean_u.npy", mean_u), ("mean_v.npy", mean_v), ("mean_eta.npy", mean_eta)):
        _require(array.shape == spatial_shape, f"重繪來源 {name} 必須是 (lat, lon)")
    _require(pc_standardized.ndim == 2 and pc_standardized.shape[1] == time_utc_ns.size, "pc_standardized.npy 必須是 (mode, time) 且與 time_utc_ns.npy 對齊")
    _require(time_utc_ns.dtype == np.int64 and time_utc_ns.ndim == 1 and time_utc_ns.size >= 2, "time_utc_ns.npy 必須是至少兩筆的 int64 一維軸")
    _require(np.all(np.diff(time_utc_ns) > 0), "重繪來源 time_utc_ns.npy 必須嚴格遞增")
    mode_count = pc_standardized.shape[0]
    expected_mode_shape = (mode_count, *spatial_shape)
    for name, array in (
        ("regression_u.npy", regression_u),
        ("regression_v.npy", regression_v),
        ("regression_eta.npy", regression_eta),
    ):
        _require(array.shape == expected_mode_shape, f"重繪來源 {name} 必須是 (mode, lat, lon)")
    _require(explained_variance.ndim == 1 and explained_variance.size == mode_count, "explained_variance.npy 必須與 PC 模態數一致")
    _require(np.all(np.isfinite(pc_standardized)), "重繪來源標準化 PC 不可含 NaN 或無限值")
    _require(np.all(np.isfinite(explained_variance)) and np.all(explained_variance >= 0), "重繪來源 explained variance 必須是有限非負值")

    representation = metadata.get("svd", {}).get("academic_visualization_representation", {})
    pc_standard_deviation = np.asarray(representation.get("pc_standard_deviation_raw_units", []), dtype=np.float64)
    _require(pc_standard_deviation.shape == (mode_count,), "來源 metadata 缺少與模態數一致的 PC 樣本標準差")
    pc_mean_max_abs = float(representation.get("pc_mean_max_abs_raw_units", 0.0))
    _require(np.all(np.isfinite(pc_standard_deviation)) and np.all(pc_standard_deviation > 0), "來源 metadata 的 PC 樣本標準差必須為有限正值")
    _require(np.isfinite(pc_mean_max_abs) and pc_mean_max_abs >= 0, "來源 metadata 的 PC 均值尾差必須為有限非負值")

    return ExistingRunFigureInputs(
        lon=lon,
        lat=lat,
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
    )


def _renderer_code_sha256() -> str:
    """雜湊實際繪圖函式原始碼，使程式改圖後自動產生新的 immutable bundle ID。"""

    return hashlib.sha256(inspect.getsource(_make_figures).encode("utf-8")).hexdigest()


def replot_surface_multivariate_svd(
    *,
    run_dir: Path,
    output_root: Path,
    config_path: Path | None = None,
) -> Path:
    """只讀既有 SVD run 並原子發布一份新的 figure bundle。

    `config_path` 可省略，此時沿用來源 run 的 `config.json`。若提供另一份完整分析設定，
    除 `figures` 外的所有欄位必須與來源設定完全相同；這允許安全調整 DPI、PNG/SVG、
    mode count 與箭頭密度，但禁止用另一個 bbox 或年份替既有陣列貼錯標籤。
    """

    performance = PerformanceRecorder()
    with performance.measure("source_and_configuration_validation"):
        resolved_run_dir = run_dir.resolve()
        resolved_output_root = output_root.resolve()
        _require(resolved_run_dir.is_dir(), f"既有 SVD run 目錄不存在: {resolved_run_dir}")
        source_metadata_path = resolved_run_dir / "metadata.json"
        source_config_path = resolved_run_dir / "config.json"
        source_metadata = _read_json_object(source_metadata_path)
        source_config_raw = _read_json_object(source_config_path)
        source_run_id = source_metadata.get("run_id")
        _require(isinstance(source_run_id, str) and source_run_id == resolved_run_dir.name, "來源 metadata.run_id 必須與 run 目錄名稱一致")
        _require(source_metadata.get("schema_name") == "ocm_surface_multivariate_svd", "重繪來源必須是正式 surface multivariate SVD run")
        render_config_path = config_path.resolve() if config_path is not None else source_config_path
        render_config: AnalysisConfig = load_analysis_config(render_config_path)
        source_science_hash = _science_config_hash(source_config_raw)
        _require(
            _science_config_hash(render_config.raw) == source_science_hash,
            "重繪設定除 figures 外必須與來源 run 完全相同；科學設定變更必須建立新的 SVD run",
        )
        source_metadata_sha256 = _sha256_file(source_metadata_path)
        renderer_code_sha256 = _renderer_code_sha256()
        bundle_digest = _canonical_json_hash(
            {
                "source_run_id": source_run_id,
                "source_metadata_sha256": source_metadata_sha256,
                "source_science_config_sha256": source_science_hash,
                "figure_config": render_config.raw["figures"],
                "renderer_code_sha256": renderer_code_sha256,
            }
        )[:12]
        safe_style = "".join(character if character.isalnum() or character in "-_" else "_" for character in render_config.figure_style)
        bundle_id = f"{safe_style}_{bundle_digest}"
        final_dir = resolved_output_root / "svd_figure_bundles" / source_run_id / bundle_id
        if final_dir.exists():
            raise FileExistsError(f"相同來源、圖面設定與 renderer 的 figure bundle 已存在，拒絕覆寫: {final_dir}")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        partial_dir = final_dir.parent / f".{bundle_id}.partial-{uuid.uuid4().hex}"
        partial_dir.mkdir(parents=False)

    try:
        with performance.measure("existing_run_array_loading"):
            inputs = _load_existing_run_figure_inputs(resolved_run_dir, source_metadata)
        with performance.measure("figure_rendering"):
            figure_files = _make_figures(
                partial_dir,
                inputs.lon,
                inputs.lat,
                inputs.mean_u,
                inputs.mean_v,
                inputs.mean_eta,
                inputs.visualization,
                inputs.time_utc_ns,
                inputs.explained_variance,
                render_config,
            )
        with performance.measure("bundle_provenance_serialization"):
            # 重繪期間若來源 metadata 被外部程序改動，停止發布。科學 run 原本就有 immutable
            # 契約；這個二次檢查再避免六區重繪與人工搬檔同時發生時產生來源不一致的 bundle。
            _require(_sha256_file(source_metadata_path) == source_metadata_sha256, "重繪期間來源 metadata 已改變，拒絕發布 figure bundle")
            _write_json(partial_dir / "figure_config.json", render_config.raw["figures"])
            bundle_metadata = {
                "schema_name": "ocm_svd_figure_bundle",
                "schema_version": FIGURE_BUNDLE_SCHEMA_VERSION,
                "status": "figures_ready",
                "bundle_id": bundle_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_run": {
                    "run_id": source_run_id,
                    "status": source_metadata.get("status"),
                    "metadata_sha256": source_metadata_sha256,
                    "science_config_sha256_excluding_figures": source_science_hash,
                    "read_policy": "read-only NumPy memory-map; source arrays are referenced, not copied",
                    "surface_cache_read": False,
                    "svd_solver_called": False,
                },
                "renderer": {
                    "style": render_config.figure_style,
                    "renderer_function": "ocm_svd_analysis.surface_multivariate_svd._make_figures",
                    "renderer_code_sha256": renderer_code_sha256,
                },
                "figure_config": render_config.raw["figures"],
                "figures": figure_files,
                "limitations": [
                    "本 bundle 只重繪既有科學陣列，沒有重新讀取 surface cache、重新插補或重新求解 SVD。",
                    "圖面科學意義與限制沿用 source_run metadata；bundle 不取代來源 run。",
                ],
            }
        bundle_metadata["performance"] = performance.to_metadata(
            scope_end=(
                "從重繪函式入口至 bundle provenance 組裝；不含最後 metadata.json"
                " 寫入與原子目錄 rename。"
            )
        )
        _write_json(partial_dir / "metadata.json", bundle_metadata)
        os.replace(partial_dir, final_dir)
    except Exception:
        # 只移除本次以 UUID 建立的未發布 bundle；來源 run 與任何已發布 bundle 均不修改。
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
    return final_dir
