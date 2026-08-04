#!/usr/bin/env bash
#
# 六區表層 SVD「全部可得樣本」時間軸預檢。
#
# 此腳本只讀取 batch 設定、每月 metadata.json 與 time_utc_ns.npy；用途是在啟動會讀取
# u/v/eta 大陣列的正式 SVD 前，先找出 partial month、UTC 倒序／重複、未套用的已知時間
# 修正或非預期採樣節奏。它不讀取流場欄位、不呼叫 SVD 求解器，也不建立任何 output 目錄。
#
# 使用方式：
#   export OCM_SURFACE_ROOT=/path/to/preprocessed/ocm_surface
#   ./scripts/preflight_surface_svd_time_axis.sh
#
# 可選參數依序為 batch JSON 與 surface cache 根目錄；適合在不同 SERVER 或測試快取重用：
#   ./scripts/preflight_surface_svd_time_axis.sh /path/to/batch.json /path/to/ocm_surface

set -euo pipefail

# 腳本以自身位置回推專案根目錄，避免使用者從 tmux、家目錄或其他工作目錄執行時找錯
# pyproject.toml、設定檔或 coastline provenance。所有 Python 匯入因而使用同一套 uv 環境。
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 共用 logger 以 EXIT trap 保存本次預檢的成功或失敗；來源 cache 路徑等實際採用值會在
# 下方寫入 context，而逐區時間統計則由內嵌 Python 以 JSON attachment 附入同一份日誌。
source "$SCRIPT_DIR/json_run_log.sh"
ocm_svd_json_log_initialize "$PROJECT_ROOT" "$(basename -- "$0")" "$@"

# 預設鎖定本次已核定的六區「2024–2025 全部可得」契約；第一參數可覆蓋，便於未來建立
# 新年份或敏感度 batch 時沿用相同預檢器，而不必複製 Python 邏輯。
BATCH_CONFIG_PATH="${1:-configs/six_regions_surface_svd_available_2024_2025_batch.json}"
SURFACE_ROOT_PATH="${2:-${OCM_SURFACE_ROOT:-}}"
ocm_svd_json_log_context "batch_config_path" "$BATCH_CONFIG_PATH"
ocm_svd_json_log_context "surface_root_path" "$SURFACE_ROOT_PATH"
PREFLIGHT_SUMMARY_PATH="$(mktemp "$PROJECT_ROOT/logs/.preflight_surface_svd_time_axis_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "preflight_summary" "$PREFLIGHT_SUMMARY_PATH"

# 資料根目錄由正式 SVD CLI 同樣以 runtime 注入，不寫死在科學 JSON。若未提供，立即以
# 可讀錯誤停止，避免 Python 將空字串解析為目前目錄並產生誤導的缺檔訊息。
if [[ -z "$SURFACE_ROOT_PATH" ]]; then
  echo "錯誤：請設定 OCM_SURFACE_ROOT，或以第二個參數提供 ocm_surface 根目錄。" >&2
  exit 2
fi

if [[ ! -f "$BATCH_CONFIG_PATH" ]]; then
  echo "錯誤：找不到 batch 設定檔：$BATCH_CONFIG_PATH" >&2
  exit 2
fi

if [[ ! -d "$SURFACE_ROOT_PATH" ]]; then
  echo "錯誤：surface cache 根目錄不存在或不是目錄：$SURFACE_ROOT_PATH" >&2
  exit 2
fi

# 使用正式管線的設定解析與內部時間驗證函式，確保預檢和實跑遵守相同的 partial-month
# 授權、known_time_axis_repairs、嚴格遞增 UTC 與中位採樣步長規則。每區錯誤會完整列出，
# 不會在第一個失敗區就中止，讓操作者一次得到六區診斷結果。
uv run --frozen --no-sync --python 3.12.13 python3 - "$BATCH_CONFIG_PATH" "$SURFACE_ROOT_PATH" "$PREFLIGHT_SUMMARY_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from ocm_svd_analysis.batch import load_batch_config
from ocm_svd_analysis.surface_multivariate_svd import (
    _apply_known_time_axis_repairs,
    _canonicalize_source_time_axis,
    _validate_month_metadata,
    _validate_source_time_axis,
)


def _write_summary(summary_path: Path, batch_config_path: Path, surface_root: Path, regions: list[dict[str, object]]) -> None:
    """將逐區預檢結果交給外層 bash JSON logger 附掛。

    此檔是 logs 內短暫的 machine-readable attachment，內容只包含設定位置、cache 根目錄與
    每區時間統計／錯誤，不含 u/v/eta 資料。外層 EXIT trap 會把它嵌入最終執行日誌後刪除，
    因此操作者只需保存一份完整 JSON 即可追溯本次預檢。
    """

    summary_path.write_text(
        json.dumps(
            {
                "batch_config_path": str(batch_config_path),
                "surface_root_path": str(surface_root),
                "regions": regions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(batch_config_path: Path, surface_root: Path, summary_path: Path) -> int:
    """逐區驗證 24 個月 UTC 軸並印出可供 SERVER log 保存的單行摘要。

    此函式刻意不呼叫 load_surface_focus_data，因為後者會讀取 u/v/eta focus 小窗。預檢
    的輸入只有 metadata 與一維 int64 時間軸，輸出為每區的中位步長、最大缺口、斷點數及
    canonicalization 的重排／去重筆數；若失敗，例外文字沿用正式管線，讓修正設定後可直接
    重跑此腳本確認。
    """

    region_results: list[dict[str, object]] = []
    try:
        batch = load_batch_config(batch_config_path)
    except Exception as error:
        region_results.append(
            {
                "status": "failed",
                "analysis_unit_id": None,
                "error": str(error),
            }
        )
        _write_summary(summary_path, batch_config_path, surface_root, region_results)
        print(f"FAIL batch_config: {error}")
        return 1

    failed = False
    for region in batch.regions:
        config = region.analysis_config
        time_chunks: list[np.ndarray] = []
        partial_month_ids: list[str] = []
        try:
            for year in config.years:
                for month in config.months:
                    month_id = f"{year}{month:02d}"
                    month_dir = surface_root / config.domain_id / "months" / month_id
                    metadata_path = month_dir / "metadata.json"
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    cache_kind = _validate_month_metadata(
                        metadata,
                        config,
                        month_id,
                        allow_partial_months=True,
                        allow_trial=False,
                    )
                    if cache_kind == "standard_partial_month":
                        partial_month_ids.append(month_id)
                    time = np.load(month_dir / "time_utc_ns.npy", mmap_mode="r", allow_pickle=False)
                    repaired_time, _ = _apply_known_time_axis_repairs(time, config, month_id)
                    time_chunks.append(repaired_time)

            merged_time = np.concatenate(time_chunks)
            canonical_time, _, canonicalization = _canonicalize_source_time_axis(merged_time, config)
            median_hours, maximum_gap_hours, gap_break_count = _validate_source_time_axis(canonical_time, config)
            partial_text = ",".join(partial_month_ids) if partial_month_ids else "none"
            print(
                f"OK   {region.analysis_unit_id} "
                f"median={median_hours:.3g}h max_gap={maximum_gap_hours:.3g}h "
                f"breaks={gap_break_count} reordered={canonicalization.reordered_time_step_count} "
                f"dropped_duplicates={canonicalization.dropped_duplicate_time_step_count} "
                f"partial_months={partial_text}"
            )
            region_results.append(
                {
                    "status": "ok",
                    "analysis_unit_id": region.analysis_unit_id,
                    "median_timestep_hours": median_hours,
                    "maximum_gap_hours": maximum_gap_hours,
                    "gap_break_count": int(gap_break_count),
                    "reordered_time_step_count": canonicalization.reordered_time_step_count,
                    "dropped_duplicate_time_step_count": canonicalization.dropped_duplicate_time_step_count,
                    "partial_month_ids": partial_month_ids,
                }
            )
        except Exception as error:
            failed = True
            print(f"FAIL {region.analysis_unit_id}: {error}")
            region_results.append(
                {
                    "status": "failed",
                    "analysis_unit_id": region.analysis_unit_id,
                    "error": str(error),
                }
            )
    _write_summary(summary_path, batch_config_path, surface_root, region_results)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("預檢器內部參數錯誤：預期 batch JSON、surface cache 根目錄與摘要 JSON")
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
PY
