#!/usr/bin/env bash
#
# 從已發布的六區 fixed-depth family 建立獨立 academic_report_ready_v8 圖包。
#
# 本腳本只讀取 fixed_depth_svd/ 下的 immutable 科學陣列，輸出一律位於
# fixed_depth_svd_figure_bundles/。它不開啟 native/surface cache、不重新垂向內插或求解
# SVD，且不會寫入表層 svd_figure_bundles/。同一 style 已存在時由 replot CLI 拒絕覆寫。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

output_root_path="${1:-${SVD_OUTPUT_ROOT:-}}"
ocm_svd_json_log_context "output_root_path" "$output_root_path"
ocm_svd_json_log_context "source_namespace" "fixed_depth_svd"
ocm_svd_json_log_context "figure_namespace" "fixed_depth_svd_figure_bundles"
if [[ -z "$output_root_path" || ! -d "$output_root_path/fixed_depth_svd" ]]; then
  ocm_svd_json_log_event "error" "fixed_depth_runs" "找不到 fixed_depth_svd 科學成果父目錄"
  echo "錯誤：請提供含 fixed_depth_svd/ 的 SVD_OUTPUT_ROOT。" >&2
  exit 2
fi

# label 與 config 成對列出，讓重繪時仍驗證除了 figures 區段外的科學設定完全一致。
configs=(
  "configs/guishan_fixed_depth_svd_available_2024_2025.json"
  "configs/gongliao_fixed_depth_svd_available_2024_2025.json"
  "configs/hsinchu_fixed_depth_svd_available_2024_2025.json"
  "configs/houwan_nmmba_fixed_depth_svd_available_2024_2025.json"
  "configs/beigan_fixed_depth_svd_available_2024_2025.json"
  "configs/nangan_fixed_depth_svd_available_2024_2025.json"
)
labels=(
  "guishan_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
  "gongliao_focus_bbox_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
  "hsinchu_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
  "houwan_nmmba_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
  "beigan_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
  "nangan_surface_reference_fixed_z_005_010_020_u_v_eta_available_2024_2025_v1"
)

for index in "${!labels[@]}"; do
  run_dir="$output_root_path/fixed_depth_svd/${labels[$index]}"
  ocm_svd_json_log_event "started" "fixed_depth_replot" "開始 ${labels[$index]} 圖包"
  uv run --frozen --no-sync --python 3.12.13 ocm-svd-fixed-depth-replot \
    --run-dir "$run_dir" \
    --output-root "$output_root_path" \
    --config "${configs[$index]}"
  ocm_svd_json_log_event "completed" "fixed_depth_replot" "完成 ${labels[$index]} 圖包"
done
