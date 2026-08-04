"""驗證專案 bash 腳本的共用 JSON 執行日誌契約。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER_PATH = PROJECT_ROOT / "scripts" / "json_run_log.sh"


class ShellJsonLoggingTest(unittest.TestCase):
    """確認 bash 成功結束時能留下單一可機讀且含 context／事件的 JSON log。"""

    def test_sourceable_logger_writes_project_logs_json_on_shell_exit(self) -> None:
        """共用 logger 不可吃掉原 shell exit status，且日誌必須保留非敏感執行證據。

        測試只在 TemporaryDirectory 建立假的 project root，避免把測試自身日誌寫入真實
        repository 的 `logs/`。以獨立 bash process source 共用 library，確認 EXIT trap
        會寫入一份 JSON、保存原始 arguments、context 與事件，並在成功時標示 completed。
        這不會啟動 uv、讀取 OCM cache 或執行任何 SVD。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            command = """
set -euo pipefail
source "$1"
ocm_svd_json_log_initialize "$2" "synthetic_runner.sh" "--sample" "中文參數"
ocm_svd_json_log_context "analysis_kind" "synthetic_log_test"
ocm_svd_json_log_event "queued" "synthetic_region" "測試事件"
"""
            completed = subprocess.run(
                ["bash", "-c", command, "--", str(LOGGER_PATH), str(temporary_root)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            log_paths = list((temporary_root / "logs").glob("synthetic_runner_*.json"))
            self.assertEqual(len(log_paths), 1)
            payload = json.loads(log_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_name"], "ocm_svd_bash_execution_log")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["arguments"], ["--sample", "中文參數"])
            self.assertEqual(payload["context"]["analysis_kind"], "synthetic_log_test")
            self.assertEqual(
                payload["events"],
                [{"status": "queued", "subject": "synthetic_region", "detail": "測試事件"}],
            )
            self.assertEqual(payload["attachments"], {})

    def test_logger_preserves_failing_exit_code_and_writes_failed_json(self) -> None:
        """失敗中的 bash 腳本也必須留下 JSON，且 logger 不可把 exit code 變成成功。

        正式預檢或 batch 可能因輸入路徑、cache metadata 或 SVD 子程序失敗；此測試以
        合成的 exit 17 模擬該情境，確認 EXIT trap 先寫入 `failed` 日誌，再把原始 code
        交還給 tmux、排程器或呼叫端，避免「有 log 但作業被誤判成功」。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            command = """
set -euo pipefail
source "$1"
ocm_svd_json_log_initialize "$2" "failing_runner.sh"
ocm_svd_json_log_event "error" "synthetic_region" "模擬失敗"
exit 17
"""
            completed = subprocess.run(
                ["bash", "-c", command, "--", str(LOGGER_PATH), str(temporary_root)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 17)
            log_paths = list((temporary_root / "logs").glob("failing_runner_*.json"))
            self.assertEqual(len(log_paths), 1)
            payload = json.loads(log_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["exit_code"], 17)
            self.assertEqual(payload["events"][0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
