"""OCM 固定物理深度流速—海面高度聯合 SVD 命令列入口。

此 CLI 同時需要 paired `ocm_native/` 與 `ocm_surface/`：前者提供逐時 `hvel/zcor`，
後者提供已對齊 1 km 規則格網的唯一自由水面高度 `eta_m`、水平內插權重與表層參考場。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .fixed_depth_multivariate_svd import run_fixed_depth_multivariate_svd


def parse_args() -> argparse.Namespace:
    """解析固定深度 family CLI 參數，不在命令列隱藏任何科學深度或遮罩政策。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/gongliao_fixed_depth_svd_available_2025.json"),
        help="固定深度 SVD JSON 設定；深度、focus、缺值與 SVD 規則均由檔案版本化。",
    )
    parser.add_argument(
        "--native-root",
        type=Path,
        required=True,
        help="前處理產物的 ocm_native 根目錄，提供 hvel/zcor。",
    )
    parser.add_argument(
        "--surface-root",
        type=Path,
        required=True,
        help="paired ocm_surface 根目錄，提供 eta_m、表層參考與水平內插計畫。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="SVD 衍生結果根目錄；流程會建立 fixed_depth_svd/<analysis_label_vN>。",
    )
    parser.add_argument(
        "--allow-partial-months",
        action="store_true",
        help="明確允許 standard_partial_month；仍保留來源缺日並檢查最大時間缺口。",
    )
    parser.add_argument(
        "--allow-trial",
        action="store_true",
        help="只供合成或短期 smoke test；trial 輸出不得當作年度科學結果。",
    )
    return parser.parse_args()


def main() -> None:
    """執行固定深度 family 並印出 immutable 成果路徑。"""

    args = parse_args()
    result = run_fixed_depth_multivariate_svd(
        config_path=args.config,
        native_root=args.native_root,
        surface_root=args.surface_root,
        output_root=args.output_root,
        allow_partial_months=args.allow_partial_months,
        allow_trial=args.allow_trial,
    )
    print(result)


if __name__ == "__main__":
    main()
