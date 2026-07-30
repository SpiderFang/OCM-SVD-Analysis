"""平行重繪多個既有 SVD run，預設最多同時處理六區。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replot_batch import replot_surface_multivariate_svd_batch


def parse_args() -> argparse.Namespace:
    """解析依提交順序配對的來源 run、可選設定與批次平行上限。

    若提供 `--config`，必須與 `--run-dir` 出現相同次數，第一份設定對第一個 run，以此
    類推。這讓六區可各自保留 bbox 等科學設定，只同步修改其 `figures` 區段。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, action="append", required=True, help="可重複六次；每次指定一個 immutable SVD run。")
    parser.add_argument("--config", type=Path, action="append", help="可選；若使用，數量與順序必須逐一對應 --run-dir。")
    parser.add_argument("--output-root", type=Path, required=True, help="所有 figure bundle 的共同衍生結果根目錄。")
    parser.add_argument("--max-concurrent-regions", type=int, default=6, help="同時重繪區域數；預設 6。")
    return parser.parse_args()


def main() -> None:
    """執行批次重繪並輸出一行可由日誌或報告程式讀取的 JSON。"""

    args = parse_args()
    result = replot_surface_multivariate_svd_batch(
        run_dirs=tuple(args.run_dir),
        output_root=args.output_root,
        max_concurrent_regions=args.max_concurrent_regions,
        config_paths=tuple(args.config) if args.config is not None else None,
    )
    print(
        json.dumps(
            {
                "visible_cpu_cores": result.visible_cpu_cores,
                "concurrent_regions_used": result.concurrent_regions_used,
                "execution_backend": result.execution_backend,
                "total_elapsed_seconds": result.total_elapsed_seconds,
                "items": [
                    {
                        "run_id": item.run_id,
                        "bundle_dir": str(item.bundle_dir),
                        "elapsed_seconds": item.elapsed_seconds,
                    }
                    for item in result.items
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

