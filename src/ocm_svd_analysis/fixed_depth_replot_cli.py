"""從既有固定深度 SVD family 重繪最新正式圖包，不重新讀取 cache 或求解 SVD。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .fixed_depth_replot import replot_fixed_depth_multivariate_svd


def parse_args() -> argparse.Namespace:
    """解析固定深度來源 family、圖包輸出根目錄與可選視覺設定。

    `--config` 若提供，除 `figures` 外必須與來源 run 的科學設定完全相同。如此可以調整
    DPI、格式或 mode 圖數量，但不能用重繪命令悄悄改變深度、bbox、共同遮罩或年份。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="含四層既有陣列與 metadata.json 的 immutable fixed-depth family run。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="衍生圖包根目錄；不會修改來源 family。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="可選完整固定深度設定；除 figures 外必須與來源 config.json 一致。",
    )
    return parser.parse_args()


def main() -> None:
    """建立 immutable 固定深度 figure bundle 並印出發布路徑。"""

    args = parse_args()
    result = replot_fixed_depth_multivariate_svd(
        run_dir=args.run_dir,
        output_root=args.output_root,
        config_path=args.config,
    )
    print(result)


if __name__ == "__main__":
    main()
