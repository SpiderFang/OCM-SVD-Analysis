"""從既有完整水柱 SVD 科學 run 重繪獨立報告圖，不重新讀快取或求解 SVD。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .water_column_replot import replot_water_column_multivariate_svd


def parse_args() -> argparse.Namespace:
    """解析來源 run、衍生圖包目錄與可選視覺設定。

    ``--run-dir`` 必須是已完成的水柱 SVD 成果目錄，而非 ``ocm_native`` 或
    ``ocm_surface`` 快取。若使用 ``--config``，程式會雜湊驗證它除 ``figures`` 外與
    來源 ``config.json`` 完全相同；這允許套用新版 PNG/SVG、岸線與圖面規格，同時禁止
    重繪時偷換年份、flow-domain、深度或 SVD 科學設定。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="已發布的 water_column_svd/<analysis_label>/，內含 metadata.json 與既有 NPY 陣列。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="衍生 figure bundle 根目錄；不得位於來源 immutable run 內。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="可選完整水柱設定；僅 figures 可與來源 run/config.json 不同。",
    )
    return parser.parse_args()


def main() -> None:
    """執行只讀水柱重繪，並將新 figure bundle 路徑輸出至 stdout。"""

    args = parse_args()
    result = replot_water_column_multivariate_svd(
        run_dir=args.run_dir,
        output_root=args.output_root,
        config_path=args.config,
    )
    print(result)


if __name__ == "__main__":
    main()
