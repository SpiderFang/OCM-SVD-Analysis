"""六層表層至 50 m 聯合流速—自由水面高度 SVD 的命令列入口。

本 CLI 將「表層、10、20、30、40、50 m 的 u/v」與唯一的自由水面高度 ``eta``
組成一次聯合 SVD。它只讀取前處理專案已發布的 ``ocm_surface`` 與 ``ocm_native``
快取，不讀原始 SCHISM NetCDF，也不會把既有表層或固定深度 SVD 成果當成輸入。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .water_column_multivariate_svd import (
    preflight_water_column_multivariate_svd,
    run_water_column_multivariate_svd,
)


def parse_args() -> argparse.Namespace:
    """解析資料根目錄、不可覆寫輸出位置與明確寬鬆資料權限。

    垂向深度、四個 flow domain 的邊界、求解器與圖面模式數均由 JSON 設定版本化，
    不接受命令列覆寫，避免同一成果目錄因隱藏參數而無法重現。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="六層聯合 SVD JSON 設定；必須鎖定上游 ocm_flow_domains.json 版本。",
    )
    parser.add_argument(
        "--native-root",
        type=Path,
        required=True,
        help="前處理產物的 ocm_native 根目錄，提供 hvel/zcor 固定深度內插。",
    )
    parser.add_argument(
        "--surface-root",
        type=Path,
        required=True,
        help="paired ocm_surface 根目錄，提供表層 u/v、eta、規則格網與水平內插權重。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="成果根目錄；正式 run 會建立 water_column_svd/<analysis_label_vN>。",
    )
    parser.add_argument(
        "--allow-partial-months",
        action="store_true",
        help="明確接受 standard_partial_month；來源時間缺口仍會完整寫入 metadata。",
    )
    parser.add_argument(
        "--allow-trial",
        action="store_true",
        help="只允許本機合成或短期 trial cache；正式兩年成果不得使用此選項。",
    )
    parser.add_argument(
        "--no-figures",
        action="store_true",
        help="只產生科學陣列與 metadata，不產生水柱獨立圖面資產；僅供開發驗證。",
    )
    parser.add_argument(
        "--resume-partial",
        type=Path,
        help=(
            "從失敗後保留的未發布 recovery 目錄續跑直接 SVD；會重新驗證設定與 canonical UTC 軸，"
            "不重新讀取兩年 native 3D 資料。不得與 --preflight 併用。"
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="只驗證 paired metadata、時間軸、格網與儲存需求估計，不寫正式成果。",
    )
    return parser.parse_args()


def main() -> None:
    """執行預檢或正式六層聯合 SVD，並輸出機器可讀的一行結果。"""

    args = parse_args()
    if args.preflight and args.resume_partial is not None:
        raise SystemExit("--preflight 不可與 --resume-partial 併用；續跑會直接驗證 checkpoint 後求解。")
    if args.preflight:
        summary = preflight_water_column_multivariate_svd(
            config_path=args.config,
            native_root=args.native_root,
            surface_root=args.surface_root,
            output_root=args.output_root,
            allow_partial_months=args.allow_partial_months,
            allow_trial=args.allow_trial,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    result = run_water_column_multivariate_svd(
        config_path=args.config,
        native_root=args.native_root,
        surface_root=args.surface_root,
        output_root=args.output_root,
        allow_partial_months=args.allow_partial_months,
        allow_trial=args.allow_trial,
        make_figures=not args.no_figures,
        resume_partial_dir=args.resume_partial,
    )
    print(result)


if __name__ == "__main__":
    main()
