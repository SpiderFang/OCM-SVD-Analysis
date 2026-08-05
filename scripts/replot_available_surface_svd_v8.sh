#!/usr/bin/env bash
# 將已發布的六區 2024–2025 全部可得 SVD 科學成果重繪為 v8 報告圖。
#
# 本腳本只讀 `$SVD_OUTPUT_ROOT/svd/<run-id>` 內既有的平均場、回歸模態、標準化 PC、
# UTC 時間軸及解釋變異量，絕不開啟 OCM surface cache、重新插補或重新求解 SVD。它以
# v8 設定建立另一份 immutable figure bundle：跨年度 PC 仍保留每個月，完整年月改為
# 270° 直式橫寫；來源科學 run 與原先 v6／v7 圖檔都保持不變。北竿／南竿直接沿用 v1
# label 更新 AOI 後，可能同時保有舊、新 run；本腳本會以排除 figures 後的完整設定精確
# 選取新 AOI run，不會以檔案時間或 glob 順序猜測。其餘四區若只有一份歷史 run，仍可
# 使用該 run 的科學設定搭配目前 v8 圖面規格重繪。

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
# 僅在確實需要呼叫 replot batch 時才建立並附加標準輸出摘要。若六區皆已發布 v8 bundle，
# 不建立空白檔，以免 JSON 日誌出現無法解析的空附件而混淆完成狀態。
summary_path=""
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

# 順序與六區 batch 設定一致；每份 JSON 的 figures.style 已固定為 v8。新 AOI 的北竿／
# 南竿必須以目前設定精確對應新 run；其餘單一歷史 run 則由下方產生只替換 figures 的
# 暫存設定，保持原科學設定不變而升級圖面。
configs=(
  "configs/guishan_surface_svd_available_2024_2025.json"
  "configs/gongliao_surface_svd_available_2024_2025.json"
  "configs/hsinchu_surface_svd_available_2024_2025.json"
  "configs/houwan_nmmba_surface_svd_available_2024_2025.json"
  "configs/beigan_surface_svd_available_2024_2025.json"
  "configs/nangan_surface_svd_available_2024_2025.json"
)


select_run_for_current_config() {
  # 回傳 `exact`、`legacy` 或 `pending` 加上 run 路徑。exact 表示 run 的 science-only
  # config 與目前 JSON 完全一致，北竿／南竿有舊、新兩份時只會選到新 AOI。legacy 僅在
  # 一個 label 只有一份歷史 run、但目前 config 只更新了跨專案 SHA 時允許，用於其餘四區。
  local config_path="$1"
  local allow_legacy_run="$2"
  uv run --frozen --no-sync --python 3.12.13 python3 - "$output_root" "$config_path" "$allow_legacy_run" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from ocm_svd_analysis.surface_multivariate_svd import _canonical_json_hash


# figures 不參與 science identity；因此本比較可在不重新求解 SVD 的前提下，辨識 AOI、
# 時間規則、遮罩、上游 SHA 或任一科學欄位是否對應目前設定。
output_root = Path(sys.argv[1])
config_path = Path(sys.argv[2])
allow_legacy_run = sys.argv[3] == "true"
current = json.loads(config_path.read_text(encoding="utf-8"))
label = current.get("analysis_label")
if not isinstance(label, str) or not label:
    raise SystemExit(f"設定缺少 analysis_label: {config_path}")
expected_hash = _canonical_json_hash({key: value for key, value in current.items() if key != "figures"})
candidates: list[Path] = []
exact_matches: list[Path] = []
for candidate in sorted((output_root / "svd").glob(f"{label}_*")):
    snapshot_path = candidate / "config.json"
    metadata_path = candidate / "metadata.json"
    if not candidate.is_dir() or not snapshot_path.is_file() or not metadata_path.is_file():
        continue
    candidates.append(candidate.resolve())
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_hash = _canonical_json_hash({key: value for key, value in snapshot.items() if key != "figures"})
    if snapshot_hash == expected_hash:
        exact_matches.append(candidate.resolve())

if len(exact_matches) == 1:
    print(f"exact\t{exact_matches[0]}")
elif len(exact_matches) > 1:
    raise SystemExit(f"{label} 有 {len(exact_matches)} 個與目前設定完全相同的 run，拒絕自動選取")
elif not candidates:
    print("pending\t")
elif len(candidates) == 1 and allow_legacy_run:
    # 唯一候選僅可作為未變更 AOI 的舊區域 fallback；呼叫端會用其 config.json 保留科學
    # 欄位，僅替換成目前 JSON 的 figures 區段，避免把新的 provenance SHA 寫回舊 run。
    print(f"legacy\t{candidates[0]}")
elif not allow_legacy_run:
    # 北竿、南竿的舊 AOI run 即使只剩一份也不可回退使用；使用者已明確棄用它，必須等候
    # 新 AOI 的 immutable science run 發布後再建立 v8 圖包。
    print("stale\t")
else:
    rendered = ", ".join(str(path) for path in candidates)
    raise SystemExit(f"{label} 找到 {len(candidates)} 個 run，但沒有一個符合目前科學設定：{rendered}")
PY
}


make_v8_render_config_for_legacy_run() {
  # 產生「舊 science config + 目前 v8 figures」暫存 JSON。重繪器會驗證 figures 外完全相同，
  # 因此此合併檔不可能拿新 bbox、時間規則或遮罩去標示舊陣列；它只更新字型與版面等圖面欄位。
  local run_dir="$1"
  local current_config_path="$2"
  local analysis_label="$3"
  local render_config_path
  render_config_path="$(mktemp "$project_root/logs/.${analysis_label}_v8_render_config_XXXXXX")"
  ocm_svd_json_log_attach_json_file "legacy_render_config_${analysis_label}" "$render_config_path"
  uv run --frozen --no-sync --python 3.12.13 python3 - "$run_dir/config.json" "$current_config_path" "$render_config_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


# science snapshot 是已發布陣列的唯一正確科學描述；目前設定只提供 v8 figures 欄位。兩者
# 合併後供 replot 驗證，確保歷史 run 不會因上游檔案 SHA 更新而被套上不同的 AOI 或遮罩。
source_config_path = Path(sys.argv[1])
current_config_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
source = json.loads(source_config_path.read_text(encoding="utf-8"))
current = json.loads(current_config_path.read_text(encoding="utf-8"))
figures = current.get("figures")
if not isinstance(figures, dict):
    raise SystemExit(f"目前設定缺少 figures 物件: {current_config_path}")
source["figures"] = figures
output_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  printf '%s\n' "$render_config_path"
}

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

  # 北竿／南竿的 v1 label 雖維持不變，但舊 AOI run 已棄用；這兩區只接受與當前設定精確
  # 相符的 run。其餘四區才可在只有一份歷史 run 時使用 legacy figures-only 重繪。
  allow_legacy_run="true"
  case "$analysis_label" in
    beigan_surface_u_v_eta_available_2024_2025_v1|nangan_surface_u_v_eta_available_2024_2025_v1)
      allow_legacy_run="false"
      ;;
  esac
  selection="$(select_run_for_current_config "$config_path" "$allow_legacy_run")"
  selection_kind="${selection%%$'\t'*}"
  run_dir="${selection#*$'\t'}"
  if [[ "$selection_kind" == "pending" || "$selection_kind" == "stale" ]]; then
    if [[ "$selection_kind" == "stale" ]]; then
      ocm_svd_json_log_event "pending" "${analysis_label}" "只找到已棄用的舊 AOI run，已忽略"
      echo "PENDING ${analysis_label}：只找到已棄用的舊 AOI run，已忽略。"
    else
      ocm_svd_json_log_event "pending" "${analysis_label}" "尚未找到已發布科學 run"
      echo "PENDING ${analysis_label}：尚未找到已發布科學 run，略過。"
    fi
    ((pending_count += 1))
    continue
  fi
  render_config_path="$config_path"
  if [[ "$selection_kind" == "legacy" ]]; then
    render_config_path="$(make_v8_render_config_for_legacy_run "$run_dir" "$config_path" "${analysis_label}")"
    ocm_svd_json_log_event "legacy_config" "${analysis_label}" "唯一歷史 run 保留其科學設定，僅套用目前 v8 figures"
  fi
  bundle_dir="$output_root/svd_figure_bundles/$(basename -- "$run_dir")/academic_report_ready_v8"
  if [[ -e "$bundle_dir" ]]; then
    ocm_svd_json_log_event "skipped" "${analysis_label}" "v8 figure bundle 已存在：$bundle_dir"
    echo "SKIP ${analysis_label}：v8 figure bundle 已存在：$bundle_dir"
    continue
  fi

  run_args+=(--run-dir "$run_dir")
  config_args+=(--config "$render_config_path")
  ocm_svd_json_log_event "queued" "${analysis_label}" "將以 $selection_kind 選取的 run 重繪：$run_dir"
done

if (( ${#run_args[@]} == 0 )); then
  ocm_svd_json_log_event "completed" "replot_batch" "沒有需要重繪的已完成區域；PENDING=${pending_count}"
  echo "沒有需要重繪的已完成區域；PENDING=${pending_count}。"
  exit 0
fi

# 批次重繪最多六區，但每區只讀自身已發布的小型科學陣列；不會因這裡的平行化重新執行
# 原本耗時的月份 I/O 或 SVD。輸出目錄由 replot 程式原子發布，既有 v6/v7 與任何完成
# 的 v8 bundle 都不會被覆寫。
summary_path="$(mktemp "$project_root/logs/.six_regions_surface_replot_v8_summary_XXXXXX")"
ocm_svd_json_log_attach_json_file "replot_batch_summary" "$summary_path"
set +e
uv run --frozen --no-sync --python 3.12.13 ocm-svd-replot-batch \
  "${run_args[@]}" \
  "${config_args[@]}" \
  --output-root "$output_root" \
  --max-concurrent-regions 6 2>&1 | tee "$summary_path"
replot_exit_code="${PIPESTATUS[0]}"
set -e

if (( replot_exit_code != 0 )); then
  ocm_svd_json_log_event "error" "replot_batch" "v8 重繪程序以 exit code $replot_exit_code 結束"
  exit "$replot_exit_code"
fi

ocm_svd_json_log_event "completed" "replot_batch" "v8 重繪程序成功結束；PENDING=${pending_count}"
echo "v8 重繪提交完成；PENDING=${pending_count}。"
