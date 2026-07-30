"""OCM SVD 命令列入口。

CLI 僅負責接收資料根目錄、輸出根目錄與明確的寬鬆權限開關；所有科學參數都放在 JSON
設定檔，以避免不同 SERVER 命令因隱藏預設值而產生不可比較的 SVD 結果。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .surface_multivariate_svd import run_surface_multivariate_svd


def parse_args() -> argparse.Namespace:
    """解析 SVD pilot 的 CLI 參數。

    `surface_root` 必須指向前處理產物中的 `ocm_surface/` 根目錄，而不是 raw NetCDF、
    `preprocessed/` 根目錄或任一月份目錄。這項限制讓讀取器可固定依照
    `<surface_root>/<flow_domain>/months/<YYYYMM>` 尋找資料，降低拿錯產品層級的風險。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/gongliao_surface_svd_2025.json"),
        help="SVD JSON 設定檔；預設為貢寮 2025 候選框 pilot。",
    )
    parser.add_argument(
        "--surface-root",
        type=Path,
        required=True,
        help="前處理產物的 ocm_surface 根目錄，例如 $OCM_SURFACE_ROOT。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="SVD 衍生結果根目錄；流程會在其下建立 svd/<run_id>。",
    )
    parser.add_argument(
        "--allow-partial-months",
        action="store_true",
        help="明確允許 standard_partial_month；仍會檢查時間缺口，不會自動補整月。",
    )
    parser.add_argument(
        "--allow-trial",
        action="store_true",
        help="只供本機 smoke test 使用 trial_ready；輸出會標記為 trial，不得作科學結論。",
    )
    parser.add_argument(
        "--no-figures",
        action="store_true",
        help="只輸出數值與 metadata，不產生平均流、模態、PC 與解釋變異圖。",
    )
    return parser.parse_args()


def main() -> None:
    """執行單一設定檔定義的 surface u/v/eta SVD run，並印出成果路徑。"""

    args = parse_args()
    result_dir = run_surface_multivariate_svd(
        config_path=args.config,
        surface_root=args.surface_root,
        output_root=args.output_root,
        allow_partial_months=args.allow_partial_months,
        allow_trial=args.allow_trial,
        make_figures=not args.no_figures,
    )
    print(result_dir)


if __name__ == "__main__":
    main()
