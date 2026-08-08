"""匯出主持人指定 eta→u→v、feature×time 的水柱 SVD 矩陣表示。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .water_column_host_layout import export_water_column_host_layout


def parse_args() -> argparse.Namespace:
    """解析已發布科學 run 與主持人版衍生輸出根目錄。

    ``--run-dir`` 只能指向已完成的 ``water_column_svd/<analysis_label>`` 成果；CLI 不接受
    native/surface cache 路徑，藉此保證轉置與特徵重排不會意外重建兩年原始資料矩陣或再次
    呼叫 SVD solver。``--output-root`` 會建立另一個 immutable 衍生成果命名空間，不能放入
    來源 run 內，以保留原本直接 SVD 的可重現性。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="已發布 water_column_svd/<analysis_label>/，內含設定模態數的 mode、PC、mask 與 metadata。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="主持人版 feature×time 因子與 round-trip 圖場的獨立輸出根目錄。",
    )
    return parser.parse_args()


def main() -> None:
    """執行無求解器、只讀來源 run 的主持人版 SVD 因子匯出。"""

    args = parse_args()
    result = export_water_column_host_layout(
        run_dir=args.run_dir,
        output_root=args.output_root,
    )
    print(result)


if __name__ == "__main__":
    main()
