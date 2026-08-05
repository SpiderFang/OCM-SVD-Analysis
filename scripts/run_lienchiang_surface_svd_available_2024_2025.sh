#!/usr/bin/env bash
# 重跑北竿與南竿更新 v1 AOI 的 2024–2025 全部可得表層 SVD。
#
# 本腳本只讀既有 `lienchiang_common_cache_v3` surface cache，不會重建前處理資料；北竿與
# 南竿 AOI 的更新已直接寫入上游 `ocm_svd_analysis_units_v1.json` 與兩份既有 v1 SVD JSON。
# 因此它以更新後設定建立新的 immutable science run，絕不覆寫原先設定雜湊不同的成果。每次
# 執行皆將完整 stdout 與 stderr 附入 `<project>/logs/` 的 JSON 日誌，以便 SERVER tmux 中斷
# 或失敗後追溯。
#
# 使用方式：先 export OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT，直接執行本檔；亦可依序以第一、
# 第二參數覆蓋兩個根目錄。若上次只完成其中一區，設定 `SVD_SKIP_EXISTING=1` 可重用已原子
# 發布且設定雜湊相同的成果，僅求解尚未完成的一區；預設不啟用，避免意外跳過新 AOI 結果。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

# 共用 logger 在 EXIT trap 寫入成功／失敗狀態、實際資料根目錄與 batch 終端輸出。這避免
# 操作者只看到 tmux 捲動訊息卻無法確認北竿、南竿各自是否已原子發布成果。
source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

batch_config_path="configs/lienchiang_surface_svd_available_2024_2025_batch.json"
surface_root_path="${1:-${OCM_SURFACE_ROOT:-}}"
output_root_path="${2:-${SVD_OUTPUT_ROOT:-}}"
skip_existing="${SVD_SKIP_EXISTING:-0}"
ocm_svd_json_log_context "batch_config_path" "$batch_config_path"
ocm_svd_json_log_context "surface_root_path" "$surface_root_path"
ocm_svd_json_log_context "output_root_path" "$output_root_path"
ocm_svd_json_log_context "allow_partial_months" "true"
ocm_svd_json_log_context "skip_existing" "$skip_existing"
batch_summary_path="$(mktemp "$project_root/logs/.lienchiang_surface_svd_batch_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "batch_terminal_output" "$batch_summary_path"

if [[ -z "$surface_root_path" || -z "$output_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "必須提供 OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT"
  echo "錯誤：請設定 OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT，或以兩個位置參數依序提供。" >&2
  exit 2
fi
if [[ ! -f "$batch_config_path" ]]; then
  ocm_svd_json_log_event "error" "batch_config" "找不到 $batch_config_path"
  echo "錯誤：找不到北竿／南竿 batch 設定：$batch_config_path" >&2
  exit 2
fi
if [[ ! -d "$surface_root_path/lienchiang_common_cache_v3" ]]; then
  ocm_svd_json_log_event "error" "surface_root" "找不到連江 flow cache：$surface_root_path/lienchiang_common_cache_v3"
  echo "錯誤：找不到連江 surface cache：$surface_root_path/lienchiang_common_cache_v3" >&2
  exit 2
fi

batch_flags=(--allow-partial-months)
if [[ "$skip_existing" == "1" ]]; then
  # 只有操作者明確指定時才重用設定雜湊相同的 immutable 成果。這是中斷後復原的窄化權限，
  # 不會讓初次重跑無聲跳過任何新 AOI 成果。
  batch_flags+=(--skip-existing)
  ocm_svd_json_log_event "resume" "svd_batch" "SVD_SKIP_EXISTING=1；設定相同的已完成 run 將被重用"
fi

ocm_svd_json_log_event "started" "svd_batch" "開始北竿與南竿更新 AOI 表層 SVD"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-batch \
  --batch-config "$batch_config_path" \
  --surface-root "$surface_root_path" \
  --output-root "$output_root_path" \
  "${batch_flags[@]}" 2>&1 | tee "$batch_summary_path"
batch_exit_code="${PIPESTATUS[0]}"
set -e

if (( batch_exit_code != 0 )); then
  ocm_svd_json_log_event "error" "svd_batch" "北竿／南竿 batch 以 exit code $batch_exit_code 結束"
  exit "$batch_exit_code"
fi

ocm_svd_json_log_event "completed" "svd_batch" "北竿與南竿更新 AOI batch 成功結束"
