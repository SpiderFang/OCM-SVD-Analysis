#!/usr/bin/env bash
#
# 啟動六區 2024–2025 fixed-depth SVD family 的分組依序執行正式批次。
#
# 此入口只呼叫 ocm-svd-fixed-depth-batch；該 CLI 僅會在 output root 內建立
# fixed_depth_svd/<analysis_label_vN>/。它不呼叫 ocm-svd-batch、不寫入 svd/，也不產生
# 表層圖包。每區完成後以 immutable family 原子發布，失敗時可用 --skip-existing 安全續跑。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

# 參數依序覆蓋 native root、surface root、output root；未提供時使用清楚命名的環境變數。
batch_config_path="configs/six_regions_fixed_depth_svd_available_2024_2025_batch.json"
native_root_path="${1:-${OCM_NATIVE_ROOT:-}}"
surface_root_path="${2:-${OCM_SURFACE_ROOT:-}}"
output_root_path="${3:-${SVD_OUTPUT_ROOT:-}}"
ocm_svd_json_log_context "batch_config_path" "$batch_config_path"
ocm_svd_json_log_context "native_root_path" "$native_root_path"
ocm_svd_json_log_context "surface_root_path" "$surface_root_path"
ocm_svd_json_log_context "output_root_path" "$output_root_path"
ocm_svd_json_log_context "result_namespace" "fixed_depth_svd"
ocm_svd_json_log_context "allow_partial_months" "true"
batch_summary_path="$(mktemp "$project_root/logs/.fixed_depth_svd_batch_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "batch_summary" "$batch_summary_path"

if [[ -z "$native_root_path" || -z "$surface_root_path" || -z "$output_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "必須提供 OCM_NATIVE_ROOT、OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT"
  echo "錯誤：請設定 OCM_NATIVE_ROOT、OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT。" >&2
  exit 2
fi
if [[ ! -f "$batch_config_path" || ! -d "$native_root_path" || ! -d "$surface_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "batch 或 paired cache 根目錄不存在"
  echo "錯誤：batch 設定、native root 或 surface root 不存在。" >&2
  exit 2
fi

# 先執行 paired 唯讀預檢；任一區無法通過就絕不開始會大量讀取 native cache 的正式 batch。
ocm_svd_json_log_event "started" "paired_preflight" "開始六區 fixed-depth paired cache 預檢"
"$script_dir/preflight_fixed_depth_svd_available_2024_2025.sh" \
  "$batch_config_path" "$native_root_path" "$surface_root_path"
ocm_svd_json_log_event "completed" "paired_preflight" "六區 fixed-depth paired cache 預檢通過"

# tee 保存 CLI 的單行 JSON 摘要；PIPESTATUS 保留 Python CLI 的真正 exit code，避免 tee
# 成功卻掩蓋任一執行組的失敗。預設拒絕既有成果，防止不小心混入舊來源；恢復時請手動加
# FIXED_DEPTH_SKIP_EXISTING=1，仍只重用 immutable fixed_depth family、不會覆寫任何目錄。
skip_existing_args=()
if [[ "${FIXED_DEPTH_SKIP_EXISTING:-0}" == "1" ]]; then
  skip_existing_args+=("--skip-existing")
  ocm_svd_json_log_context "skip_existing" "true"
else
  ocm_svd_json_log_context "skip_existing" "false"
fi
ocm_svd_json_log_event "started" "fixed_depth_batch" "開始六區 2024–2025 fixed-depth 分組依序執行批次"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-fixed-depth-batch \
  --batch-config "$batch_config_path" \
  --native-root "$native_root_path" \
  --surface-root "$surface_root_path" \
  --output-root "$output_root_path" \
  --allow-partial-months \
  "${skip_existing_args[@]}" | tee "$batch_summary_path"
batch_exit_code="${PIPESTATUS[0]}"
set -e

if (( batch_exit_code != 0 )); then
  ocm_svd_json_log_event "error" "fixed_depth_batch" "batch CLI 以 exit code $batch_exit_code 結束"
  exit "$batch_exit_code"
fi
ocm_svd_json_log_event "completed" "fixed_depth_batch" "六區 fixed-depth batch 成功結束"
