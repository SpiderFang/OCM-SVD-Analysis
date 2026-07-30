"""六區表層 u/v/eta SVD 平行批次的命令列入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .batch import run_surface_multivariate_svd_batch


def parse_args() -> argparse.Namespace:
    """解析多區 SVD batch 的資料根目錄、輸出根目錄與明確寬鬆開關。

    研究區域、年份、每區 CPU 核數與平行數均鎖在 JSON；CLI 不提供覆蓋選項，避免有人在
    SERVER 命令列臨時把六區的受控 24 核策略改成過度超額配置而沒有可追溯紀錄。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-config",
        type=Path,
        default=Path("configs/six_regions_surface_svd_2025_batch.json"),
        help="六區 SVD batch JSON；預設為 2025 年 v1 六區計畫。",
    )
    parser.add_argument("--surface-root", type=Path, required=True, help="前處理產物的 ocm_surface 根目錄。")
    parser.add_argument("--output-root", type=Path, required=True, help="SVD 衍生結果根目錄。")
    parser.add_argument("--allow-partial-months", action="store_true", help="明確接受 standard_partial_month；時間缺口仍會依單區設定驗證。")
    parser.add_argument("--allow-trial", action="store_true", help="只供本機 smoke test 使用 trial_ready，正式 SERVER 不應使用。")
    parser.add_argument("--no-figures", action="store_true", help="只輸出數值與 metadata，不繪製六區圖面。")
    parser.add_argument("--skip-existing", action="store_true", help="明確重用不可覆寫的既有 run；未加此旗標時偵測到既有成果會停止。")
    return parser.parse_args()


def main() -> None:
    """執行 batch 並輸出一行可供日誌收集的 JSON 摘要。"""

    args = parse_args()
    result = run_surface_multivariate_svd_batch(
        batch_config_path=args.batch_config,
        surface_root=args.surface_root,
        output_root=args.output_root,
        allow_partial_months=args.allow_partial_months,
        allow_trial=args.allow_trial,
        make_figures=not args.no_figures,
        skip_existing=args.skip_existing,
    )
    print(
        json.dumps(
            {
                "batch_label": result.batch_label,
                "visible_cpu_cores": result.visible_cpu_cores,
                "concurrent_regions_used": result.concurrent_regions_used,
                "execution_backend": result.execution_backend,
                "total_elapsed_seconds": result.total_elapsed_seconds,
                "regions": [
                    {
                        "analysis_unit_id": region.analysis_unit_id,
                        "status": region.status,
                        "result_dir": str(region.result_dir) if region.result_dir is not None else None,
                        "elapsed_seconds": region.elapsed_seconds,
                    }
                    for region in result.regions
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
