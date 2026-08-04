"""六區固定深度 SVD「分組依序執行」批次的命令列入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fixed_depth_batch import run_fixed_depth_multivariate_svd_batch


def parse_args() -> argparse.Namespace:
    """解析 paired cache 根目錄與 immutable fixed-depth batch 控制選項。

    年份、深度、AOI、執行組、CPU/I/O 上限都只能由版本化 JSON 提供；CLI 不允許覆蓋，
    避免操作人員在 SERVER 臨時把固定深度結果導向表層 ``svd/`` 或改變科學契約。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-config",
        type=Path,
        default=Path("configs/six_regions_fixed_depth_svd_available_2024_2025_batch.json"),
        help="固定深度六區分組依序執行的 batch JSON；不可使用表層 batch 設定。",
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
        help="paired ocm_surface 根目錄，提供 eta 與表層參考。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="固定深度衍生結果根目錄；只會建立 fixed_depth_svd/。",
    )
    parser.add_argument(
        "--allow-partial-months",
        action="store_true",
        help="明確接受 standard_partial_month；仍完整記錄來源缺口與 canonicalization。",
    )
    parser.add_argument(
        "--allow-trial",
        action="store_true",
        help="只供短期 smoke test；正式雙年度成果不可使用。",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="只重用已原子發布的 fixed-depth family；從不覆寫既有成果。",
    )
    return parser.parse_args()


def main() -> None:
    """執行分組依序的 batch 並輸出單行 JSON，供 bash logger 保存作業證據。"""

    args = parse_args()
    result = run_fixed_depth_multivariate_svd_batch(
        batch_config_path=args.batch_config,
        native_root=args.native_root,
        surface_root=args.surface_root,
        output_root=args.output_root,
        allow_partial_months=args.allow_partial_months,
        allow_trial=args.allow_trial,
        skip_existing=args.skip_existing,
    )
    print(
        json.dumps(
            {
                "batch_label": result.batch_label,
                "result_namespace": result.result_namespace,
                "figure_bundle_namespace": result.figure_bundle_namespace,
                "visible_cpu_cores": result.visible_cpu_cores,
                "execution_backend": result.execution_backend,
                "total_elapsed_seconds": result.total_elapsed_seconds,
                "regions": [
                    {
                        "execution_group_id": region.execution_group_id,
                        "analysis_unit_id": region.analysis_unit_id,
                        "status": region.status,
                        "result_dir": (
                            str(region.result_dir)
                            if region.result_dir is not None
                            else None
                        ),
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
