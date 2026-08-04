#!/usr/bin/env bash
# 將已發布的六區 2024–2025 全部可得 SVD 科學成果重繪為 v8 報告圖。
#
# 本腳本只讀 `$SVD_OUTPUT_ROOT/svd/<run-id>` 內既有的平均場、回歸模態、標準化 PC、
# UTC 時間軸及解釋變異量，絕不開啟 OCM surface cache、重新插補或重新求解 SVD。它以
# v8 設定建立另一份 immutable figure bundle：跨年度 PC 仍保留每個月，完整年月改為
# 270° 直式橫寫；來源科學 run 與原先 v6／v7 圖檔都保持不變。若某區尚未完成，會標示
# PENDING；若同一 analysis label 對到多個科學 run，會停止要求人工判定，避免任意選取
# 成果而誤配圖面。

set -euo pipefail
shopt -s nullglob

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

# 每次重繪都以 EXIT trap 建立一份專案內 JSON 日誌。逐區 PENDING／SKIP／QUEUED 事件會
# 保留在 log，讓 tmux 離線後仍可判定哪些成果已重繪、哪些只是尚未完成科學 run。
source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

output_root="${1:-${SVD_OUTPUT_ROOT:-}}"
ocm_svd_json_log_context "output_root" "$output_root"
ocm_svd_json_log_context "figure_style" "academic_report_ready_v8"
if [[ -z "$output_root" ]]; then
  ocm_svd_json_log_event "error" "output_root" "未提供 SVD_OUTPUT_ROOT 或第一個命令列參數"
  echo "用法：$0 <SVD_OUTPUT_ROOT>；或先 export SVD_OUTPUT_ROOT=..." >&2
  exit 2
fi
if [[ ! -d "$output_root/svd" ]]; then
  ocm_svd_json_log_event "error" "output_root" "找不到既有科學成果目錄：$output_root/svd"
  echo "找不到既有科學成果目錄：$output_root/svd" >&2
  exit 2
fi

# 順序與六區 batch 設定一致；每份 JSON 的 figures.style 已固定為 v8。把配置檔視為
# 圖面 provenance 的一部分，不能從來源 v6/v7 config 自動推測或臨時覆寫，才能保留
# 版本化的可追溯性。
configs=(
  "configs/guishan_surface_svd_available_2024_2025.json"
  "configs/gongliao_surface_svd_available_2024_2025.json"
  "configs/hsinchu_surface_svd_available_2024_2025.json"
  "configs/houwan_nmmba_surface_svd_available_2024_2025.json"
  "configs/beigan_surface_svd_available_2024_2025.json"
  "configs/nangan_surface_svd_available_2024_2025.json"
)

run_args=()
config_args=()
pending_count=0
for config_path in "${configs[@]}"; do
  if [[ ! -f "$config_path" ]]; then
    ocm_svd_json_log_event "error" "$config_path" "缺少 v8 重繪設定"
    echo "ERROR 缺少 v8 重繪設定：$config_path" >&2
    exit 2
  fi

  # analysis_label 是 SVD run 目錄名前綴；設定檔屬 repository 控管的 JSON，這裡只擷取
  # 該單行字串，不寫回設定，也不依 shell pattern 改變任何成果目錄。
  analysis_label="$(sed -n 's/^[[:space:]]*"analysis_label"[[:space:]]*:[[:space:]]*"\([^"]*\)"[[:space:]]*,[[:space:]]*$/\1/p' "$config_path")"
  if [[ -z "$analysis_label" ]]; then
    ocm_svd_json_log_event "error" "$config_path" "無法讀取 analysis_label"
    echo "ERROR 無法從設定讀取 analysis_label：$config_path" >&2
    exit 2
  fi

  matching_runs=("$output_root/svd/${analysis_label}"_*)
  if (( ${#matching_runs[@]} == 0 )); then
    ocm_svd_json_log_event "pending" "$analysis_label" "尚未找到已發布科學 run"
    echo "PENDING $analysis_label：尚未找到已發布科學 run，略過。"
    ((pending_count += 1))
    continue
  fi
  if (( ${#matching_runs[@]} != 1 )); then
    ocm_svd_json_log_event "error" "$analysis_label" "找到 ${#matching_runs[@]} 個候選科學 run，拒絕自動選取"
    echo "ERROR $analysis_label：找到 ${#matching_runs[@]} 個候選科學 run，拒絕自動選取：" >&2
    printf '  %s\n' "${matching_runs[@]}" >&2
    exit 2
  fi

  run_dir="${matching_runs[0]}"
  bundle_dir="$output_root/svd_figure_bundles/$(basename -- "$run_dir")/academic_report_ready_v8"
  if [[ -e "$bundle_dir" ]]; then
    ocm_svd_json_log_event "skipped" "$analysis_label" "v8 figure bundle 已存在：$bundle_dir"
    echo "SKIP $analysis_label：v8 figure bundle 已存在：$bundle_dir"
    continue
  fi

  run_args+=(--run-dir "$run_dir")
  config_args+=(--config "$config_path")
  ocm_svd_json_log_event "queued" "$analysis_label" "將重繪 $run_dir"
done

if (( ${#run_args[@]} == 0 )); then
  ocm_svd_json_log_event "completed" "replot_batch" "沒有需要重繪的已完成區域；PENDING=$pending_count"
  echo "沒有需要重繪的已完成區域；PENDING=$pending_count。"
  exit 0
fi

# 批次重繪最多六區，但每區只讀自身已發布的小型科學陣列；不會因這裡的平行化重新執行
# 原本耗時的月份 I/O 或 SVD。輸出目錄由 replot 程式原子發布，既有 v6/v7 與任何完成
# 的 v8 bundle 都不會被覆寫。
uv run --frozen --no-sync --python 3.12.13 ocm-svd-replot-batch \
  "${run_args[@]}" \
  "${config_args[@]}" \
  --output-root "$output_root" \
  --max-concurrent-regions 6

ocm_svd_json_log_event "completed" "replot_batch" "v8 重繪程序成功結束；PENDING=$pending_count"
echo "v8 重繪提交完成；PENDING=$pending_count。"
