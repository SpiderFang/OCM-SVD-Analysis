#!/usr/bin/env bash
#
# 六區 2024–2025 fixed-depth SVD 的 paired native/surface 唯讀預檢。
#
# 此腳本在任何大型 hvel/zcor 讀取與 SVD 前，逐區檢查 24 個月的 paired cache metadata、
# time_utc_ns、grid source-node 對應與陣列 shape。它不建立 fixed_depth_svd 結果目錄，
# 不讀取原始 SCHISM NetCDF，也不操作表層 svd/ 或 svd_figure_bundles/；唯一寫入的是
# 專案 logs/ 的 JSON 作業證據。

set -euo pipefail

# 以檔案位置決定專案根目錄，讓 tmux、排程器與互動 shell 都載入相同的 uv 專案與設定。
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

# 預設固定深度 batch；三個位置參數可供不同 SERVER 或鏡像快取重用，但不可改變科學設定。
batch_config_path="${1:-configs/six_regions_fixed_depth_svd_available_2024_2025_batch.json}"
native_root_path="${2:-${OCM_NATIVE_ROOT:-}}"
surface_root_path="${3:-${OCM_SURFACE_ROOT:-}}"
ocm_svd_json_log_context "batch_config_path" "$batch_config_path"
ocm_svd_json_log_context "native_root_path" "$native_root_path"
ocm_svd_json_log_context "surface_root_path" "$surface_root_path"
preflight_summary_path="$(mktemp "$project_root/logs/.preflight_fixed_depth_svd_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "preflight_summary" "$preflight_summary_path"

if [[ -z "$native_root_path" || -z "$surface_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "必須提供 OCM_NATIVE_ROOT 與 OCM_SURFACE_ROOT"
  echo "錯誤：請設定 OCM_NATIVE_ROOT 與 OCM_SURFACE_ROOT，或以第二、第三個位置參數提供。" >&2
  exit 2
fi
if [[ ! -f "$batch_config_path" || ! -d "$native_root_path" || ! -d "$surface_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "batch 或 paired cache 根目錄不存在"
  echo "錯誤：batch 設定、native root 或 surface root 不存在。" >&2
  exit 2
fi

# 內嵌 Python 只重用正式固定深度 loader 的驗證規則；它以 mmap 開啟檔頭檢查 shape，
# 不將 hvel/zcor 或 u/v/eta 實體化到 RAM。每區獨立記錄錯誤，讓一次預檢就能診斷全部六區。
uv run --frozen --no-sync --python 3.12.13 python3 - \
  "$batch_config_path" "$native_root_path" "$surface_root_path" "$preflight_summary_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from ocm_svd_analysis.fixed_depth_batch import load_fixed_depth_batch_config
from ocm_svd_analysis.fixed_depth_multivariate_svd import _load_fixed_depth_grid
from ocm_svd_analysis.surface_multivariate_svd import (
    _apply_known_time_axis_repairs,
    _canonicalize_source_time_axis,
    _validate_month_metadata,
    _validate_source_time_axis,
)


def write_summary(path: Path, batch_path: Path, native_root: Path, surface_root: Path, regions: list[dict[str, object]]) -> None:
    """將不含流場數值的 paired 檢查摘要交給外層 JSON logger。

    結果保存每區 grid/node 契約、partial 月份、UTC canonicalization 與錯誤文字；不寫入
    任一 SVD 輸出命名空間，避免預檢被誤當成科學成果或污染 immutable family 目錄。
    """

    path.write_text(
        json.dumps(
            {
                "batch_config_path": str(batch_path),
                "native_root_path": str(native_root),
                "surface_root_path": str(surface_root),
                "result_namespace": "fixed_depth_svd",
                "regions": regions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def require(condition: bool, message: str) -> None:
    """以清楚錯誤中止單一區檢查，外層仍會繼續檢查其餘區域。"""

    if not condition:
        raise ValueError(message)


def check_region(region, native_root: Path, surface_root: Path) -> dict[str, object]:
    """驗證一區 24 個月份的 paired cache 介面，不讀取完整動態場。

    固定深度正式 runner 會讀取 native hvel/zcor 並在 focus source nodes 做垂向內插；預檢
    先確認每個月份的時間軸、成對 config hash、grid source node 與所有必要動態陣列 shape
    均符合此 runner 的輸入契約。垂向 coverage 與 SVD 數值本身留給 smoke/正式 run 計算。
    """

    config = region.config
    (
        _lon,
        _lat,
        _area,
        _static,
        _geometry,
        _lat_slice,
        _lon_slice,
        selected_nodes,
        _local_vertices,
        _weights,
    ) = _load_fixed_depth_grid(surface_root, native_root, config)
    domain = config.base.domain_id
    surface_grid = surface_root / domain / "grid"
    full_lat = int(np.load(surface_grid / "lat.npy", mmap_mode="r", allow_pickle=False).size)
    full_lon = int(np.load(surface_grid / "lon.npy", mmap_mode="r", allow_pickle=False).size)
    source_times: list[np.ndarray] = []
    partial_month_ids: list[str] = []
    repaired_time_step_count = 0
    checked_months: list[str] = []
    for year in config.base.years:
        for month in config.base.months:
            month_id = f"{year}{month:02d}"
            surface_month = surface_root / domain / "months" / month_id
            native_month = native_root / domain / "months" / month_id
            surface_metadata = json.loads((surface_month / "metadata.json").read_text(encoding="utf-8"))
            native_metadata = json.loads((native_month / "metadata.json").read_text(encoding="utf-8"))
            surface_kind = _validate_month_metadata(surface_metadata, config.base, month_id, allow_partial_months=True, allow_trial=False)
            native_kind = _validate_month_metadata(native_metadata, config.base, month_id, allow_partial_months=True, allow_trial=False)
            require(surface_kind == native_kind, f"{month_id} paired cache_kind 不一致")
            require(surface_metadata.get("config_hash") == native_metadata.get("config_hash"), f"{month_id} paired config_hash 不一致")
            if surface_kind == "standard_partial_month":
                partial_month_ids.append(month_id)
            surface_time = np.load(surface_month / "time_utc_ns.npy", mmap_mode="r", allow_pickle=False)
            native_time = np.load(native_month / "time_utc_ns.npy", mmap_mode="r", allow_pickle=False)
            require(surface_time.dtype == np.int64 and native_time.dtype == np.int64 and np.array_equal(surface_time, native_time), f"{month_id} paired time_utc_ns 必須逐值相同")
            repaired_time, repaired_count = _apply_known_time_axis_repairs(surface_time, config.base, month_id)
            repaired_time_step_count += repaired_count
            source_times.append(repaired_time)
            expected_surface_shape = (surface_time.size, full_lat, full_lon)
            for filename in ("u_surface_mps.npy", "v_surface_mps.npy", "eta_m.npy"):
                array = np.load(surface_month / filename, mmap_mode="r", allow_pickle=False)
                require(array.shape == expected_surface_shape and np.issubdtype(array.dtype, np.floating), f"{month_id} {filename} 不是預期的浮點 (time,lat,lon)")
            valid = np.load(surface_month / "valid_mask_surface.npy", mmap_mode="r", allow_pickle=False)
            require(valid.dtype == np.bool_ and valid.shape == expected_surface_shape, f"{month_id} valid_mask_surface.npy shape/dtype 不正確")
            hvel = np.load(native_month / "hvel.npy", mmap_mode="r", allow_pickle=False)
            zcor = np.load(native_month / "zcor.npy", mmap_mode="r", allow_pickle=False)
            require(hvel.ndim == 4 and hvel.shape[0] == surface_time.size and hvel.shape[-1] >= 2 and np.issubdtype(hvel.dtype, np.floating), f"{month_id} hvel shape/dtype 不符合 fixed-depth 契約")
            require(zcor.shape == hvel.shape[:3] and np.issubdtype(zcor.dtype, np.floating), f"{month_id} zcor 必須與 hvel 的前三維對齊")
            require(int(selected_nodes[-1]) < hvel.shape[1], f"{month_id} focus source node 超出 native hvel node 軸")
            checked_months.append(month_id)
    source_time = np.concatenate(source_times)
    canonical_time, _indices, canonicalization = _canonicalize_source_time_axis(source_time, config.base)
    median_hours, maximum_gap_hours, gap_break_count = _validate_source_time_axis(canonical_time, config.base)
    return {
        "status": "ok",
        "analysis_unit_id": region.analysis_unit_id,
        "flow_domain_id": domain,
        "result_namespace": "fixed_depth_svd",
        "checked_month_ids": checked_months,
        "selected_source_node_count": int(selected_nodes.size),
        "partial_month_ids": partial_month_ids,
        "repaired_time_step_count": repaired_time_step_count,
        "median_timestep_hours": median_hours,
        "maximum_gap_hours": maximum_gap_hours,
        "gap_break_count": int(gap_break_count),
        "time_axis_canonicalization": {
            "policy": canonicalization.policy,
            "input_time_count": canonicalization.input_time_count,
            "output_time_count": canonicalization.output_time_count,
            "reordered_time_step_count": canonicalization.reordered_time_step_count,
            "dropped_duplicate_time_step_count": canonicalization.dropped_duplicate_time_step_count,
        },
    }


def main(batch_path: Path, native_root: Path, surface_root: Path, summary_path: Path) -> int:
    """執行全部區域預檢，任何失敗皆回報但不建立 fixed-depth 成果。"""

    results: list[dict[str, object]] = []
    try:
        batch = load_fixed_depth_batch_config(batch_path)
    except Exception as error:
        results.append({"status": "failed", "analysis_unit_id": None, "error": str(error)})
        write_summary(summary_path, batch_path, native_root, surface_root, results)
        print(f"FAIL fixed-depth batch_config: {error}")
        return 1
    failed = False
    for execution_group in batch.execution_groups:
        for region in execution_group.regions:
            try:
                result = check_region(region, native_root, surface_root)
                result["execution_group_id"] = execution_group.execution_group_id
                results.append(result)
                print(f"OK   {region.analysis_unit_id} 執行組={execution_group.execution_group_id} nodes={result['selected_source_node_count']} max_gap={result['maximum_gap_hours']:.3g}h")
            except Exception as error:
                failed = True
                results.append({"status": "failed", "analysis_unit_id": region.analysis_unit_id, "execution_group_id": execution_group.execution_group_id, "error": str(error)})
                print(f"FAIL {region.analysis_unit_id}: {error}")
    write_summary(summary_path, batch_path, native_root, surface_root, results)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit("預檢器內部參數錯誤：預期 batch、native root、surface root、摘要 JSON")
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
PY
