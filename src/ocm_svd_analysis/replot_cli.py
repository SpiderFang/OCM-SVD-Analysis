"""從既有 SVD 科學 run 重繪白底完整標示報告圖，不重新讀資料或求解 SVD。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .replot import replot_surface_multivariate_svd


def parse_args() -> argparse.Namespace:
    """解析單區重繪來源、figure bundle 根目錄與可選視覺設定。

    `--config` 必須是完整分析設定，但只允許 `figures` 區段不同。使用完整設定而不是零散
    CLI 覆蓋值，可讓六區報告的 DPI、格式與 mode count 仍有一份可保存、可稽核的 JSON。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="含 metadata.json 與既有分析陣列的 immutable SVD run。")
    parser.add_argument("--output-root", type=Path, required=True, help="figure bundle 衍生結果根目錄。")
    parser.add_argument(
        "--config",
        type=Path,
        help="可選的完整分析設定；除 figures 外必須與來源 run/config.json 完全相同。",
    )
    return parser.parse_args()


def main() -> None:
    """建立 immutable figure bundle 並印出成果路徑。"""

    args = parse_args()
    result_dir = replot_surface_multivariate_svd(
        run_dir=args.run_dir,
        output_root=args.output_root,
        config_path=args.config,
    )
    print(result_dir)


if __name__ == "__main__":
    main()
