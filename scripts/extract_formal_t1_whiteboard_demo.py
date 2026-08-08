#!/usr/bin/env python3
"""從正式水柱 SVD 因子建立兩個正式深度層的 t1 展示欄。

本腳本只讀正式 run 已發布的 mean、physical mode、PC、mask、座標與奇異值，不讀 raw cache、
不重建兩年矩陣，也不修改正式成果。它使用第一個保留 UTC 時次的正式前 20 模態重建：

    q_rank20(t1) = mean + physical_mode × PC(t1)

接著保留正式完整 102×152 格網的 mask，只展示表層與 10 m 兩個速度層，並依
eta → u(surface) → u(10m) → v(surface) → v(10m) 建立一欄。輸出不是 toy 數值，也不宣稱
是未截斷原始觀測欄；metadata 會保存正式來源、UTC 時次與 rank-20 限制。
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np


LEVEL_IDS = ("surface", "z_minus_010m")
LEVEL_LABELS = ("surface", "10m")


def read_json(path: Path) -> dict:
    """讀取正式 run JSON，並拒絕非 object 內容。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必須是 object：{path}")
    return value


def append_masked_block(
    records: list[dict[str, object]],
    *,
    component: str,
    level_index: int,
    mask: np.ndarray,
    mean_field: np.ndarray,
    mode_field: np.ndarray,
    pc_t1: np.ndarray,
    scale_field: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
) -> None:
    """對正式 mask=True 的所有 row-major 格點建立 host feature row。

    不使用裁切的小格網：每一個正式有效格點都保留其 102×152 全域 row/col。mask=False 的
    陸地、海床以下或固定深度不可用位置不會出現在 CSV，也不會進入示範欄。
    """

    rows, cols = np.nonzero(mask)
    for row, col in zip(rows, cols):
        row_index = int(row)
        col_index = int(col)
        physical_mode = np.asarray(mode_field[:, row_index, col_index], dtype=np.float64)
        physical_mean = float(mean_field[row_index, col_index])
        physical_t1 = physical_mean + float(physical_mode @ pc_t1)
        feature_scale = float(scale_field[row_index, col_index])
        records.append(
            {
                "component": component,
                "level_index": level_index,
                "level_id": "eta_surface" if component == "eta" else LEVEL_IDS[level_index],
                "depth_label": "eta" if component == "eta" else LEVEL_LABELS[level_index],
                "grid_row_zero_based": row_index,
                "grid_col_zero_based": col_index,
                "longitude": float(lon[col_index]),
                "latitude": float(lat[row_index]),
                "physical_mean": physical_mean,
                "feature_scale": feature_scale,
                "physical_t1_rank20": physical_t1,
                "weighted_centered_t1_rank20": (physical_t1 - physical_mean) * feature_scale,
            }
        )


def write_feature_map(path: Path, records: list[dict[str, object]]) -> None:
    """寫出正式 t1 展示欄的 row-to-grid 對照表。"""

    fields = (
        "host_feature_index_zero_based",
        "host_feature_index_one_based",
        "component",
        "level_index",
        "level_id",
        "depth_label",
        "grid_row_zero_based",
        "grid_col_zero_based",
        "longitude",
        "latitude",
        "physical_mean",
        "feature_scale",
        "physical_t1_rank20",
        "weighted_centered_t1_rank20",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, record in enumerate(records):
            writer.writerow(
                {
                    **record,
                    "host_feature_index_zero_based": index,
                    "host_feature_index_one_based": index + 1,
                }
            )


def write_json(path: Path, payload: dict) -> None:
    """寫出含正式來源與限制的 metadata。"""

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_whiteboard(path: Path, records: list[dict[str, object]], metadata: dict, error: float) -> None:
    """寫出可直接照抄的正式 t1 欄位順序與回填步驟。"""

    counts: dict[str, int] = {}
    for record in records:
        key = f"{record['component']}_{record['depth_label']}"
        counts[key] = counts.get(key, 0) + 1
    lines = [
        "正式兩年 SVD 因子衍生的 t1 展示欄（完整 102×152 格網、兩個正式速度層）",
        "",
        f"source analysis label = {metadata['analysis_label']}",
        f"source first retained UTC ns = {metadata['source_t1_utc_ns']}",
        "本欄物理值 = formal mean + formal physical mode × formal PC(t1)。",
        "所以不是 toy 數值；但它是正式前 20 模態的 rank-20 重建，不是未截斷原始 t1 欄。",
        "",
        "A_t1_demo[:,0] 的列順序：",
        f"  eta（{counts.get('eta_eta', 0)} 格）",
        f"  u surface（{counts.get('u_surface', 0)} 格）",
        f"  u 10m（{counts.get('u_10m', 0)} 格）",
        f"  v surface（{counts.get('v_surface', 0)} 格）",
        f"  v 10m（{counts.get('v_10m', 0)} 格）",
        f"  shape = ({len(records)}, 1)",
        "",
        "單欄展示 SVD：A_t1_demo = U[:,0] × sigma[0] × Vh[0,:]",
        "  只有一個非零奇異值；此欄只證明主持人的排列與回填方式。",
        "",
        "回填：讀 feature_index_map.csv，依 component、depth_label、grid_row、grid_col 放回",
        "      完整 102×152 的 eta、u(surface)、u(10m)、v(surface)、v(10m) 五個圖場。",
        f"  weighted SVD round-trip 最大絕對誤差 = {error:.12g}",
        "",
        "正式成果仍是全矩陣 A=[a(t1)|...|a(t17052)] 的前 20 個直接 SVD 模態。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract_formal_t1_demo(run_dir: Path, output_dir: Path) -> Path:
    """從正式 run 只讀抽出正式兩層、完整格網的 t1 展示結果。"""

    run_dir = run_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"拒絕覆寫既有展示輸出：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = read_json(run_dir / "metadata.json")
    read_json(run_dir / "config.json")
    if metadata.get("schema_name") != "ocm_water_column_multivariate_svd":
        raise ValueError("來源必須是正式 water_column_multivariate_svd run")

    lon = np.load(run_dir / "lon.npy", mmap_mode="r", allow_pickle=False)
    lat = np.load(run_dir / "lat.npy", mmap_mode="r", allow_pickle=False)
    area = np.load(run_dir / "cell_area_m2.npy", mmap_mode="r", allow_pickle=False)
    time_utc_ns = np.load(run_dir / "time_utc_ns.npy", mmap_mode="r", allow_pickle=False)
    velocity_mask = np.load(run_dir / "velocity_feature_mask.npy", mmap_mode="r", allow_pickle=False)
    eta_mask = np.load(run_dir / "eta_feature_mask.npy", mmap_mode="r", allow_pickle=False)
    mean_u = np.load(run_dir / "mean_u_mps.npy", mmap_mode="r", allow_pickle=False)
    mean_v = np.load(run_dir / "mean_v_mps.npy", mmap_mode="r", allow_pickle=False)
    mean_eta = np.load(run_dir / "mean_eta_m.npy", mmap_mode="r", allow_pickle=False)
    mode_u = np.load(run_dir / "mode_u_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)
    mode_v = np.load(run_dir / "mode_v_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)
    mode_eta = np.load(run_dir / "mode_eta_m_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)
    pc_t1 = np.load(run_dir / "pc.npy", mmap_mode="r", allow_pickle=False)[:, 0]
    singular_values = np.load(run_dir / "singular_values.npy", mmap_mode="r", allow_pickle=False)

    svd_metadata = metadata["svd"]
    velocity_rms = float(svd_metadata["velocity_rms_mps"])
    eta_rms = float(svd_metadata["eta_rms_m"])
    vertical_metadata = metadata["vertical_sampling"]
    weights = tuple(float(value) for value in vertical_metadata["vertical_quadrature_weights_m"][:2])
    eta_scale = np.sqrt(np.asarray(area, dtype=np.float64)) / eta_rms
    velocity_scales = [
        np.sqrt(np.asarray(area, dtype=np.float64) * weight) / velocity_rms
        for weight in weights
    ]

    records: list[dict[str, object]] = []
    append_masked_block(
        records,
        component="eta",
        level_index=-1,
        mask=np.asarray(eta_mask, dtype=bool),
        mean_field=mean_eta,
        mode_field=mode_eta,
        pc_t1=pc_t1,
        scale_field=eta_scale,
        lon=lon,
        lat=lat,
    )
    for level_index in range(2):
        append_masked_block(
            records,
            component="u",
            level_index=level_index,
            mask=np.asarray(velocity_mask[level_index], dtype=bool),
            mean_field=mean_u[level_index],
            mode_field=mode_u[:, level_index],
            pc_t1=pc_t1,
            scale_field=velocity_scales[level_index],
            lon=lon,
            lat=lat,
        )
    for level_index in range(2):
        append_masked_block(
            records,
            component="v",
            level_index=level_index,
            mask=np.asarray(velocity_mask[level_index], dtype=bool),
            mean_field=mean_v[level_index],
            mode_field=mode_v[:, level_index],
            pc_t1=pc_t1,
            scale_field=velocity_scales[level_index],
            lon=lon,
            lat=lat,
        )

    weighted_column = np.asarray(
        [record["weighted_centered_t1_rank20"] for record in records],
        dtype=np.float64,
    )[:, None]
    left, demo_singular_values, right = np.linalg.svd(weighted_column, full_matrices=False)
    reconstructed_weighted = left @ np.diag(demo_singular_values) @ right
    error = float(np.max(np.abs(reconstructed_weighted - weighted_column)))

    np.save(output_dir / "A_t1_weighted_rank20_feature_by_time.npy", weighted_column, allow_pickle=False)
    np.save(output_dir / "left_singular_vectors_demo.npy", left, allow_pickle=False)
    np.save(output_dir / "singular_values_demo.npy", demo_singular_values, allow_pickle=False)
    np.save(output_dir / "right_singular_vectors_time_demo.npy", right, allow_pickle=False)
    np.save(output_dir / "roundtrip_A_t1_weighted_rank20.npy", reconstructed_weighted, allow_pickle=False)
    write_feature_map(output_dir / "feature_index_map.csv", records)

    output_maps = {
        "eta": np.full((lat.size, lon.size), np.nan, dtype=np.float64),
        "u_surface": np.full((lat.size, lon.size), np.nan, dtype=np.float64),
        "u_10m": np.full((lat.size, lon.size), np.nan, dtype=np.float64),
        "v_surface": np.full((lat.size, lon.size), np.nan, dtype=np.float64),
        "v_10m": np.full((lat.size, lon.size), np.nan, dtype=np.float64),
    }
    for record in records:
        key = "eta" if record["component"] == "eta" else f"{record['component']}_{record['depth_label']}"
        output_maps[key][int(record["grid_row_zero_based"]), int(record["grid_col_zero_based"])] = float(record["physical_t1_rank20"])
    for key, array in output_maps.items():
        np.save(output_dir / f"roundtrip_{key}_physical_t1_rank20.npy", array, allow_pickle=False)

    derived_metadata = {
        "status": "formal_factor_t1_demo_complete",
        "source_run": str(run_dir),
        "analysis_label": metadata.get("analysis_label"),
        "source_t1_utc_ns": int(time_utc_ns[0]),
        "source_formal_mode_count": int(singular_values.size),
        "source_semantics": "formal_top20_rank20_reconstruction_at_first_retained_utc",
        "grid": {"shape": [int(lat.size), int(lon.size)], "mask_policy": "formal source masks; no spatial crop"},
        "matrix": {
            "orientation": "feature_by_time",
            "shape": [len(records), 1],
            "state_vector_order": "eta_once_then_u_surface_u_10m_then_v_surface_v_10m",
        },
        "demo_svd": {
            "solver": "numpy_linalg_svd_on_one_formal_rank20_column",
            "mode_count": 1,
            "singular_value": float(demo_singular_values[0]),
        },
        "roundtrip_max_abs_error": error,
        "raw_original_t1_available": False,
        "limitation": "此欄由正式前20模態重建，不是未截斷原始兩年矩陣的原始 t1 欄；正式全矩陣與20模態成果不變。",
    }
    write_json(output_dir / "metadata.json", derived_metadata)
    write_whiteboard(output_dir / "WHITEBOARD.txt", records, derived_metadata, error)
    return output_dir


def main() -> None:
    """解析正式 run 與新輸出目錄，執行只讀 t1 抽取。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="正式 water_column_multivariate_svd run。")
    parser.add_argument("--output-dir", type=Path, required=True, help="新的正式 t1 展示輸出目錄；拒絕覆寫。")
    args = parser.parse_args()
    print(extract_formal_t1_demo(args.run_dir, args.output_dir))


if __name__ == "__main__":
    main()
