#!/usr/bin/env bash
# 啟動六區 2024–2025 全部可得表層 SVD，並保存 JSON 執行日誌。
#
# 本腳本是正式 SERVER batch 的唯一 bash 入口：它固定使用 available 契約、明確接受
# standard_partial_month，並將 batch CLI 成功時輸出的六區摘要 JSON 附入
# `<project>/logs/` 的執行日誌。資料篩選、UTC canonicalization、缺口處理與 SVD 本身仍
# 由 Python CLI 執行；此腳本不修改 OCM cache，亦不覆寫已發布的 immutable science run。
#
# 使用方式：先 export OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT，再直接執行本檔；也可依序
# 以第一、第二參數覆蓋 surface root 與 output root，方便不同 SERVER／測試位置重用。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

batch_config_path="configs/six_regions_surface_svd_available_2024_2025_batch.json"
surface_root_path="${1:-${OCM_SURFACE_ROOT:-}}"
output_root_path="${2:-${SVD_OUTPUT_ROOT:-}}"
ocm_svd_json_log_context "batch_config_path" "$batch_config_path"
ocm_svd_json_log_context "surface_root_path" "$surface_root_path"
ocm_svd_json_log_context "output_root_path" "$output_root_path"
ocm_svd_json_log_context "allow_partial_months" "true"
batch_summary_path="$(mktemp "$project_root/logs/.surface_svd_batch_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "batch_summary" "$batch_summary_path"

if [[ -z "$surface_root_path" || -z "$output_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "必須提供 OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT"
  echo "錯誤：請設定 OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT，或以兩個位置參數依序提供。" >&2
  exit 2
fi
if [[ ! -f "$batch_config_path" ]]; then
  ocm_svd_json_log_event "error" "batch_config" "找不到 $batch_config_path"
  echo "錯誤：找不到 batch 設定：$batch_config_path" >&2
  exit 2
fi
if [[ ! -d "$surface_root_path" ]]; then
  ocm_svd_json_log_event "error" "surface_root" "不存在：$surface_root_path"
  echo "錯誤：surface cache 根目錄不存在：$surface_root_path" >&2
  exit 2
fi

# tee 保留原本 CLI 的單行 batch JSON 於 tmux，同時另存為 attachment。暫時解除 `set -e`
# 才能取得 uv（而非 tee）的真實 exit code；後續仍以該 code 結束，EXIT trap 因而可靠
# 將失敗狀態寫入最終 JSON log。
ocm_svd_json_log_event "started" "svd_batch" "開始六區 available 2024–2025 SVD"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-batch \
  --batch-config "$batch_config_path" \
  --surface-root "$surface_root_path" \
  --output-root "$output_root_path" \
  --allow-partial-months 2>&1 | tee "$batch_summary_path"
batch_exit_code="${PIPESTATUS[0]}"
set -e

if (( batch_exit_code != 0 )); then
  ocm_svd_json_log_event "error" "svd_batch" "batch CLI 以 exit code $batch_exit_code 結束"
  exit "$batch_exit_code"
fi

ocm_svd_json_log_event "completed" "svd_batch" "六區 batch CLI 成功結束"
