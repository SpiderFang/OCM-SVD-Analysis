#!/usr/bin/env bash
# OCM-SVD-Analysis bash 腳本的共用 JSON 執行紀錄工具。
#
# 本檔只能由專案內的可執行腳本 `source`，不應直接執行。每次腳本開始時建立
# `<project>/logs/<script>_<UTC>_<pid>.json`，並在 EXIT trap 觸發時原子寫入開始／結束 UTC、
# 參數、執行結果、額外 context 與逐區事件。使用 Python 標準函式庫序列化可正確處理中文、
# 空白與引號，避免以 printf 手刻 JSON 導致日誌無法解析；它不會記錄環境中的密碼、token 或
# 任意完整環境變數。所有暫存事件檔都只位於 logs 目錄，成功或失敗後會移除，只保留最終 JSON。

OCM_SVD_JSON_LOG_ARGUMENTS=()
OCM_SVD_JSON_LOG_EVENT_FILE=""
OCM_SVD_JSON_LOG_CONTEXT_FILE=""
OCM_SVD_JSON_LOG_ATTACHMENT_FILE=""
OCM_SVD_JSON_LOG_FILE=""
OCM_SVD_JSON_LOG_PROJECT_ROOT=""
OCM_SVD_JSON_LOG_SCRIPT_NAME=""
OCM_SVD_JSON_LOG_STARTED_AT_UTC=""
OCM_SVD_JSON_LOG_SHELL_PID=""


ocm_svd_json_log_event() {
  # 將可讀的流程事件保存在 TSV 暫存檔；最終由 Python 轉為 JSON array，因此 stdout
  # 仍可保留給使用者與 tmux 即時判讀。事件內容禁止含 tab／換行，避免破壞這個短暫格式。
  local event_status="$1"
  local subject="$2"
  local detail="$3"
  printf '%s\t%s\t%s\n' "$event_status" "$subject" "$detail" >> "$OCM_SVD_JSON_LOG_EVENT_FILE"
}


ocm_svd_json_log_context() {
  # context 是本次腳本的非敏感輸入，例如 output root 或 batch 設定位置。它會與 CLI
  # arguments 分開保存，讓後續比對不用從命令列字串猜測實際採用的預設值。
  local key="$1"
  local value="$2"
  printf '%s\t%s\n' "$key" "$value" >> "$OCM_SVD_JSON_LOG_CONTEXT_FILE"
}


ocm_svd_json_log_attach_json_file() {
  # 較大型或巢狀的結果（例如六區預檢逐區統計）先由主流程寫成暫存 JSON，再附掛到
  # 最終日誌的指定 key。若主流程在寫入前失敗，最終 log 仍會保留 exit code 並標示附件
  # 不存在，而不會因記錄失敗掩蓋原始錯誤。
  local key="$1"
  local json_path="$2"
  printf '%s\t%s\n' "$key" "$json_path" >> "$OCM_SVD_JSON_LOG_ATTACHMENT_FILE"
}


ocm_svd_json_log_write() {
  # EXIT trap 呼叫此函式；Python 只使用標準函式庫，把由 bash 收集的結構化資料寫成單一
  # UTF-8 JSON 檔。即使科學程式失敗，這個寫入也不應改變原本 exit status。
  local exit_code="$1"
  local ended_at_utc
  # 呼叫端可沒有位置參數；在 `set -u` 下直接展開空陣列會被 Bash 視為未綁定。此處
  # 暫時關閉 nounset 僅影響 EXIT trap，確保「沒有 CLI 參數的失敗腳本」仍能完整記錄。
  set +u
  ended_at_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  python3 - \
    "$OCM_SVD_JSON_LOG_FILE" \
    "$exit_code" \
    "$OCM_SVD_JSON_LOG_SCRIPT_NAME" \
    "$OCM_SVD_JSON_LOG_STARTED_AT_UTC" \
    "$ended_at_utc" \
    "$OCM_SVD_JSON_LOG_PROJECT_ROOT" \
    "$OCM_SVD_JSON_LOG_SHELL_PID" \
    "$OCM_SVD_JSON_LOG_EVENT_FILE" \
    "$OCM_SVD_JSON_LOG_CONTEXT_FILE" \
    "$OCM_SVD_JSON_LOG_ATTACHMENT_FILE" \
    "${OCM_SVD_JSON_LOG_ARGUMENTS[@]}" <<'PY'
from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


(
    log_path_raw,
    exit_code_raw,
    script_name,
    started_at_utc,
    ended_at_utc,
    project_root,
    shell_pid,
    event_path_raw,
    context_path_raw,
    attachment_path_raw,
    *arguments,
) = sys.argv[1:]


def read_tsv(path: Path, expected_columns: int) -> list[list[str]]:
    """讀取 bash 暫存事件資料；格式損毀的列會略過，避免日誌機制掩蓋原作業結果。"""

    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        columns = line.split("\t")
        if len(columns) == expected_columns:
            rows.append(columns)
    return rows


events = [
    {"status": status, "subject": subject, "detail": detail}
    for status, subject, detail in read_tsv(Path(event_path_raw), 3)
]
context = {key: value for key, value in read_tsv(Path(context_path_raw), 2)}
attachments: dict[str, object] = {}
for key, raw_path in read_tsv(Path(attachment_path_raw), 2):
    attachment_path = Path(raw_path)
    if not attachment_path.is_file():
        attachments[key] = {"status": "missing", "path": raw_path}
        continue
    try:
        attachments[key] = json.loads(attachment_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        attachments[key] = {"status": "unreadable", "path": raw_path, "error": str(error)}

started = datetime.fromisoformat(started_at_utc.replace("Z", "+00:00"))
ended = datetime.fromisoformat(ended_at_utc.replace("Z", "+00:00"))
payload = {
    "schema_name": "ocm_svd_bash_execution_log",
    "schema_version": "1.0.0",
    "script": script_name,
    "status": "completed" if int(exit_code_raw) == 0 else "failed",
    "exit_code": int(exit_code_raw),
    "started_at_utc": started_at_utc,
    "ended_at_utc": ended_at_utc,
    "elapsed_seconds": (ended - started).total_seconds(),
    "project_root": project_root,
    "host": socket.gethostname(),
    "shell_pid": int(shell_pid),
    "arguments": arguments,
    "context": context,
    "events": events,
    "attachments": attachments,
}
log_path = Path(log_path_raw)
temporary_path = log_path.with_suffix(".partial")
temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary_path.replace(log_path)
PY
}


ocm_svd_json_log_on_exit() {
  # 先解除 trap，避免 Python 或 rm 的任何非零狀態再次遞迴進入 EXIT trap；最後明確回傳
  # 原本 bash 腳本的 exit code，讓自動化排程仍能可靠判斷成功或失敗。
  local exit_code="$1"
  trap - EXIT
  set +e
  ocm_svd_json_log_write "$exit_code"
  while IFS=$'\t' read -r _attachment_key attachment_path; do
    [[ -n "${attachment_path:-}" ]] && rm -f -- "$attachment_path"
  done < "$OCM_SVD_JSON_LOG_ATTACHMENT_FILE" 2>/dev/null
  rm -f -- "$OCM_SVD_JSON_LOG_EVENT_FILE" "$OCM_SVD_JSON_LOG_CONTEXT_FILE" "$OCM_SVD_JSON_LOG_ATTACHMENT_FILE"
  exit "$exit_code"
}


ocm_svd_json_log_initialize() {
  # 呼叫端必須先確定 project root，才能把日誌固定寫入 repository 的 logs/，不依賴使用者
  # 當下所在目錄。檔名含 UTC 與 shell PID，平行 tmux／batch 啟動時不會互相覆寫。
  local project_root="$1"
  local script_name="$2"
  shift 2

  local log_dir="$project_root/logs"
  mkdir -p "$log_dir"
  OCM_SVD_JSON_LOG_PROJECT_ROOT="$project_root"
  OCM_SVD_JSON_LOG_SCRIPT_NAME="$script_name"
  OCM_SVD_JSON_LOG_STARTED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  OCM_SVD_JSON_LOG_SHELL_PID="$$"
  OCM_SVD_JSON_LOG_ARGUMENTS=("$@")
  OCM_SVD_JSON_LOG_FILE="$log_dir/${script_name%.sh}_$(date -u +%Y%m%dT%H%M%SZ)_$$.json"
  # BSD 與 GNU mktemp 都要求 X 序列位於 template 最後，故暫存檔不附副檔名；最終正式
  # 日誌仍固定為 .json。此設計避免 macOS 把 `XXXXXX.tsv` 當成常數檔名而造成併發覆寫。
  OCM_SVD_JSON_LOG_EVENT_FILE="$(mktemp "$log_dir/.${script_name%.sh}_events_XXXXXX")"
  OCM_SVD_JSON_LOG_CONTEXT_FILE="$(mktemp "$log_dir/.${script_name%.sh}_context_XXXXXX")"
  OCM_SVD_JSON_LOG_ATTACHMENT_FILE="$(mktemp "$log_dir/.${script_name%.sh}_attachments_XXXXXX")"
  trap 'ocm_svd_json_log_on_exit "$?"' EXIT
}
