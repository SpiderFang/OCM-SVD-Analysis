#!/usr/bin/env bash
# 啟動後灣／海生館完整 flow domain 的 2024–2025 六層聯合直接 SVD。
#
# 本腳本先以唯讀 preflight 驗證 paired surface/native 月份、UTC 時軸與磁碟需求，再以同一份
# 版本化設定建立一個 immutable `water_column_svd/<analysis_label_vN>/` 成果。科學狀態向量
# 固定為表層、10、20、30、40、50 m 的 u/v 與一份 eta；它不會改寫上游 cache、不會建立
# 協方差矩陣，也不會啟動其它三個 flow domain。後灣成果完成及審查前，不應自行擴大執行範圍。

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

source "$script_dir/json_run_log.sh"
ocm_svd_json_log_initialize "$project_root" "$(basename -- "$0")" "$@"

# 三個位置參數依序覆蓋 paired native root、surface root、成果 root；使用環境變數時，
# 僅取資料根目錄而不記錄任何敏感 shell 設定到 JSON log。
config_path="configs/houwan_nmmba_flow_domain_water_column_svd_available_2024_2025.json"
native_root_path="${1:-${OCM_NATIVE_ROOT:-}}"
surface_root_path="${2:-${OCM_SURFACE_ROOT:-}}"
output_root_path="${3:-${SVD_OUTPUT_ROOT:-}}"
# 只有 Python checkpoint 驗證通過的 recovery 目錄才可略過兩年 native 3D I/O。環境變數
# 不指定時維持完整新 run；指定時仍先做 paired preflight，再把路徑顯式傳給 CLI，不能暗中
# 自動尋找任何舊暫存目錄。
resume_partial_path="${SVD_RESUME_PARTIAL:-}"
resume_arguments=()
if [[ -n "$resume_partial_path" ]]; then
  resume_arguments=(--resume-partial "$resume_partial_path")
fi
ocm_svd_json_log_context "config_path" "$config_path"
ocm_svd_json_log_context "native_root_path" "$native_root_path"
ocm_svd_json_log_context "surface_root_path" "$surface_root_path"
ocm_svd_json_log_context "output_root_path" "$output_root_path"
ocm_svd_json_log_context "result_namespace" "water_column_svd"
ocm_svd_json_log_context "analysis_scope" "houwan_full_flow_domain_only"
ocm_svd_json_log_context "allow_partial_months" "true"
ocm_svd_json_log_context "resume_partial_path" "$resume_partial_path"

if [[ -z "$native_root_path" || -z "$surface_root_path" || -z "$output_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "必須提供 OCM_NATIVE_ROOT、OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT"
  echo "錯誤：請設定 OCM_NATIVE_ROOT、OCM_SURFACE_ROOT 與 SVD_OUTPUT_ROOT。" >&2
  exit 2
fi
if [[ ! -f "$config_path" || ! -d "$native_root_path" || ! -d "$surface_root_path" ]]; then
  ocm_svd_json_log_event "error" "runtime_paths" "設定檔或 paired cache 根目錄不存在"
  echo "錯誤：設定檔、native root 或 surface root 不存在。" >&2
  exit 2
fi

# preflight 的 JSON 摘要會附入本次執行日誌，作為資料時間範圍、候選矩陣大小與預定求解
# 策略的可審查證據。失敗時不會開始數十個月份的 native 3D I/O。
preflight_summary_path="$(mktemp "$project_root/logs/.houwan_water_column_preflight_XXXXXX")"
ocm_svd_json_log_attach_json_file "preflight" "$preflight_summary_path"
ocm_svd_json_log_event "started" "preflight" "開始後灣完整 flow-domain paired cache 與資源預檢"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-water-column \
  --config "$config_path" \
  --native-root "$native_root_path" \
  --surface-root "$surface_root_path" \
  --output-root "$output_root_path" \
  --allow-partial-months \
  --preflight 2>&1 | tee "$preflight_summary_path"
preflight_exit_code="${PIPESTATUS[0]}"
set -e
if (( preflight_exit_code != 0 )); then
  ocm_svd_json_log_event "error" "preflight" "preflight CLI 以 exit code $preflight_exit_code 結束"
  exit "$preflight_exit_code"
fi
ocm_svd_json_log_event "completed" "preflight" "paired cache、時間軸及資源預檢通過"

# 正式 run 依設定求取 100 個 mode，但預設只產生前 20 個 mode 的水柱獨立圖面資產；
# 每個繪製 mode 包含六張速度空間圖、一張 eta 圖與一張 PC 圖，速度圖另有透明／內嵌比例尺衍生檔，`tee` 保留 immutable
# 成果目錄於 tmux 螢幕與 bash log。特意不提供跳過圖面的環境開關，避免正式後灣結果遺漏
# 研究需求指定的獨立圖面。若 CLI 失敗，stdout/stderr 會改名保留在 logs/，因 tmux session
# 結束後不能再依 pane history 追查 PROPACK traceback；成功時才刪除這份短暫文字檔，避免
# 日誌累積重複的正常輸出。
run_stdout_path="$(mktemp "$project_root/logs/.houwan_water_column_run_XXXXXX")"
ocm_svd_json_log_event "started" "water_column_direct_svd" "開始後灣完整 flow-domain 六層聯合直接 SVD 與水柱獨立圖面"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-water-column \
  --config "$config_path" \
  --native-root "$native_root_path" \
  --surface-root "$surface_root_path" \
  --output-root "$output_root_path" \
  --allow-partial-months \
  "${resume_arguments[@]}" 2>&1 | tee "$run_stdout_path"
run_exit_code="${PIPESTATUS[0]}"
set -e
if (( run_exit_code != 0 )); then
  # 失敗文字檔含 Python traceback 與（若已有 checkpoint）recovery 目錄；它只記錄本次 CLI
  # 訊息，不含密碼或 shell 環境。保留它可判斷應直接用 --resume-partial 重試，或需修正設定。
  failed_stdout_path="${run_stdout_path}.failed.log"
  mv -- "$run_stdout_path" "$failed_stdout_path"
  ocm_svd_json_log_context "failed_stdout_path" "$failed_stdout_path"
  ocm_svd_json_log_event "error" "water_column_direct_svd" "正式 CLI 以 exit code $run_exit_code 結束；traceback 保留於 $failed_stdout_path"
  exit "$run_exit_code"
fi
final_run_directory="$(tail -n 1 "$run_stdout_path")"
ocm_svd_json_log_context "final_run_directory" "$final_run_directory"
rm -f -- "$run_stdout_path"
ocm_svd_json_log_event "completed" "water_column_direct_svd" "後灣完整 flow-domain 成果已原子發布；尚未啟動其它 domain"
