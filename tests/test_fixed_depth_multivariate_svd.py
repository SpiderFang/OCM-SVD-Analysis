"""固定深度 `u(z)/v(z)/eta` family 的科學契約與端到端測試。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ocm_svd_analysis.fixed_depth_multivariate_svd import (
    interpolate_velocity_to_fixed_z,
    run_fixed_depth_multivariate_svd,
)
from ocm_svd_analysis.fixed_depth_replot import (
    replot_fixed_depth_multivariate_svd,
)


def _write_json(path: Path, payload: object) -> None:
    """以穩定 UTF-8 JSON 建立合成設定與 metadata。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _make_fixed_depth_config(root: Path, coastline_sha256: str) -> Path:
    """建立單月、三個固定深度且要求表層共同遮罩的最小設定。"""

    config = {
        "schema_version": "1.0.0",
        "analysis_kind": "fixed_depth_multivariate_svd",
        "analysis_label": "synthetic_fixed_depth_family_v1",
        "purpose": "測試固定深度流速與 paired eta 的共同遮罩 SVD。",
        "focus": {
            "focus_id": "synthetic_focus",
            "name_zh": "合成研究區",
            "approval_status": "approved",
            "flow_domain_id": "synthetic_domain",
            "bbox_lon_lat": [120.01, 120.03, 24.01, 24.03],
            "bbox_order": "lon_min, lon_max, lat_min, lat_max",
            "anchor_lonlat": [120.02, 24.02],
            "analysis_unit_id": "synthetic_fixed_z_v1",
            "source_analysis_units_config": "synthetic.json",
            "source_analysis_units_config_sha256": "a" * 64,
            "spatial_mask_policy": "analysis_bbox_cell_center",
        },
        "input": {
            "year": 2025,
            "months": [1],
            "required_cache_schema_major": 3,
            "required_status": "ready",
            "required_cache_kinds": ["standard_month"],
            "expected_timestep_hours": 1.0,
            "maximum_source_gap_hours": 2.0,
            "known_time_axis_repairs": [],
        },
        "fixed_depth": {
            "depths_m_below_vertical_datum": [5.0, 10.0, 20.0],
            "target_z_formula": "target_z_m = -depth_m_below_vertical_datum",
            "vertical_interpolation": "linear_between_bracketing_finite_zcor_no_extrapolation",
            "eta_source": "paired_ocm_surface_eta_m",
            "eta_vertical_interpolation": False,
            "include_surface_reference": True,
            "comparison_mask_policy": "intersection_with_surface",
        },
        "mask_and_missing_data": {
            "static_ocean_mask": "mask_static.npy",
            "minimum_static_ocean_cells": 8,
            "minimum_cell_triplet_valid_fraction": 0.95,
            "max_consecutive_missing_timesteps_to_interpolate": 0,
            "minimum_retained_time_fraction": 0.95,
        },
        "svd": {
            "variables": [
                "u_fixed_depth_mps",
                "v_fixed_depth_mps",
                "eta_m",
            ],
            "anomaly_reference": "shared synthetic samples",
            "normalization": "u_v_share_area_weighted_vector_rms__eta_uses_area_weighted_rms",
            "spatial_weight": "sqrt(cell_area_m2), repeated for u, v, eta",
            "requested_mode_count": 5,
            "minimum_reported_mode_count": 3,
            "sign_convention": "anchor u loading positive; if its magnitude is numerically negligible, use anchor v, then eta, then the largest absolute loading",
        },
        "parallel_execution": {
            "io_workers": 1,
            "linear_algebra_threads": 2,
            "policy": "合成測試只使用一個月份 worker。",
        },
        "figures": {
            "style": "academic_report_ready_v6",
            "mode_count": 3,
            "max_quiver_arrows_per_axis": 8,
            # 同時產出 SVG，讓測試可直接檢查向量圖保留的標題文字，而不必對 PNG
            # 做不穩定的 OCR；實際像素輸出仍由 PNG 存在性斷言覆蓋。
            "output_formats": ["png", "svg"],
            "raster_dpi": 72,
            "coastline_geojson": str(root / "land.geojson"),
            "coastline_geojson_sha256": coastline_sha256,
        },
    }
    path = root / "fixed_depth_config.json"
    _write_json(path, config)
    return path


def _make_paired_cache(root: Path) -> tuple[Path, Path, np.ndarray]:
    """建立 5×5 規則格與 25-node native cache，回傳 surface/native root 與 eta。

    每個規則格直接由同索引 source node 支撐，重心權重為 `(1,0,0)`。中央 3×3 中只有
    左上格的水柱底部淺於 -20 m，故共同遮罩應由 9 格縮成 8 格；這能驗證深度 coverage
    確實限制表層參考，而不是每個 level 暗中使用不同空間母體。
    """

    surface_root = root / "ocm_surface"
    native_root = root / "ocm_native"
    domain = "synthetic_domain"
    surface_grid = surface_root / domain / "grid"
    native_grid = native_root / domain / "grid"
    surface_month = surface_root / domain / "months" / "202501"
    native_month = native_root / domain / "months" / "202501"
    for directory in (surface_grid, native_grid, surface_month, native_month):
        directory.mkdir(parents=True, exist_ok=True)

    lon = np.linspace(120.0, 120.04, 5, dtype=np.float64)
    lat = np.linspace(24.0, 24.04, 5, dtype=np.float64)
    area = np.full((5, 5), 1_000_000.0, dtype=np.float64)
    static = np.ones((5, 5), dtype=bool)
    vertices = np.empty((5, 5, 3), dtype=np.int32)
    weights = np.zeros((5, 5, 3), dtype=np.float32)
    for row in range(5):
        for column in range(5):
            node = row * 5 + column
            vertices[row, column] = (node, node, node)
            weights[row, column, 0] = 1.0
    for filename, array in {
        "lon.npy": lon,
        "lat.npy": lat,
        "cell_area_m2.npy": area,
        "mask_static.npy": static,
        "source_vertices.npy": vertices,
        "source_weights.npy": weights,
    }.items():
        np.save(surface_grid / filename, array, allow_pickle=False)
    _write_json(
        surface_grid / "metadata.json",
        {
            "cache_schema_version": "3.0.0",
            "domain": {"domain_id": domain},
            "config_hash": "paired-synthetic",
        },
    )
    np.save(
        native_grid / "source_node_global_index.npy",
        np.arange(25, dtype=np.int64),
        allow_pickle=False,
    )
    _write_json(
        native_grid / "metadata.json",
        {
            "cache_schema_version": "3.0.0",
            "domain": {"domain_id": domain},
            "config_hash": "paired-synthetic",
            "node_count": 25,
        },
    )

    time_count = 48
    time = (
        np.datetime64("2025-01-01T00:00:00", "ns").astype(np.int64)
        + np.arange(time_count, dtype=np.int64) * 3_600_000_000_000
    )
    rng = np.random.default_rng(20250731)
    eta = rng.normal(scale=0.2, size=(time_count, 5, 5)).astype(np.float32)
    u_surface = rng.normal(scale=0.4, size=(time_count, 5, 5)).astype(np.float32)
    v_surface = rng.normal(scale=0.4, size=(time_count, 5, 5)).astype(np.float32)
    valid_surface = np.ones((time_count, 5, 5), dtype=bool)
    for filename, array in {
        "time_utc_ns.npy": time,
        "u_surface_mps.npy": u_surface,
        "v_surface_mps.npy": v_surface,
        "eta_m.npy": eta,
        "valid_mask_surface.npy": valid_surface,
    }.items():
        np.save(surface_month / filename, array, allow_pickle=False)

    z_levels = np.array([-30.0, -20.0, -10.0, 0.0], dtype=np.float32)
    zcor = np.broadcast_to(
        z_levels,
        (time_count, 25, z_levels.size),
    ).copy()
    # 中央分析格中的 local node 6 對 -20 m 沒有下方包夾層，因此整個 family 必須排除。
    zcor[:, 6, :] = np.array([-15.0, -10.0, -5.0, 0.0], dtype=np.float32)
    hvel = rng.normal(
        scale=0.5,
        size=(time_count, 25, z_levels.size, 2),
    ).astype(np.float32)
    for filename, array in {
        "time_utc_ns.npy": time,
        "hvel.npy": hvel,
        "zcor.npy": zcor,
    }.items():
        np.save(native_month / filename, array, allow_pickle=False)

    common_metadata = {
        "status": "ready",
        "cache_kind": "standard_month",
        "cache_schema_version": "3.0.0",
        "config_hash": "paired-synthetic",
        "domain": {"domain_id": domain},
        "month": "202501",
    }
    _write_json(surface_month / "metadata.json", common_metadata)
    _write_json(native_month / "metadata.json", common_metadata)
    return surface_root, native_root, eta


class FixedDepthMultivariateSvdTest(unittest.TestCase):
    """驗證固定 z 內插、paired eta 與共同樣本 family 輸出。"""

    def test_vertical_interpolation_brackets_exact_and_rejects_extrapolation(self) -> None:
        """線性剖面應精確內插，落在水柱外的目標必須保留 NaN。"""

        zcor = np.array([[[ -30.0, -20.0, -10.0, 0.0 ]]], dtype=np.float64)
        hvel = np.zeros((1, 1, 4, 2), dtype=np.float64)
        hvel[..., 0] = zcor * 2.0
        hvel[..., 1] = -zcor

        u, v, span = interpolate_velocity_to_fixed_z(hvel, zcor, -15.0)
        self.assertAlmostEqual(float(u[0, 0]), -30.0)
        self.assertAlmostEqual(float(v[0, 0]), 15.0)
        self.assertAlmostEqual(float(span[0, 0]), 10.0)

        exact_u, exact_v, exact_span = interpolate_velocity_to_fixed_z(
            hvel,
            zcor,
            -20.0,
        )
        self.assertAlmostEqual(float(exact_u[0, 0]), -40.0)
        self.assertAlmostEqual(float(exact_v[0, 0]), 20.0)
        self.assertAlmostEqual(float(exact_span[0, 0]), 0.0)

        outside_u, outside_v, outside_span = interpolate_velocity_to_fixed_z(
            hvel,
            zcor,
            -35.0,
        )
        self.assertTrue(np.isnan(outside_u[0, 0]))
        self.assertTrue(np.isnan(outside_v[0, 0]))
        self.assertTrue(np.isnan(outside_span[0, 0]))

    def test_family_uses_paired_eta_and_shared_surface_depth_mask(self) -> None:
        """四個 level 應共用 8 格、48 時次，且 mean_eta 必須來自同一 paired eta。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coastline = {
                "type": "FeatureCollection",
                "features": [],
            }
            coastline_path = root / "land.geojson"
            _write_json(coastline_path, coastline)
            config_path = _make_fixed_depth_config(
                root,
                hashlib.sha256(coastline_path.read_bytes()).hexdigest(),
            )
            surface_root, native_root, eta = _make_paired_cache(root)
            result = run_fixed_depth_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "derived",
            )
            # 科學目錄只保留人可讀的版本號；完整科學內容雜湊改存 metadata。
            self.assertEqual(result.name, "synthetic_fixed_depth_family_v1")
            with self.assertRaisesRegex(FileExistsError, "提升 analysis_label"):
                run_fixed_depth_multivariate_svd(
                    config_path=config_path,
                    native_root=native_root,
                    surface_root=surface_root,
                    output_root=root / "derived",
                )

            metadata = json.loads(
                (result / "metadata.json").read_text(encoding="utf-8")
            )
            shared_mask = np.load(
                result / "shared_valid_mask.npy",
                allow_pickle=False,
            )
            self.assertEqual(
                metadata["eta_source"]["native_origin"],
                "SCHISM elev(time,node) [m]",
            )
            self.assertFalse(
                metadata["eta_source"]["vertical_interpolation"]
            )
            self.assertEqual(
                metadata["shared_sample_contract"]["common_ocean_cell_count"],
                8,
            )
            self.assertEqual(int(np.count_nonzero(shared_mask)), 8)
            self.assertEqual(
                metadata["shared_sample_contract"]["retained_time_count"],
                48,
            )
            self.assertEqual(len(metadata["levels"]), 4)
            self.assertEqual(len(metadata["science_provenance_sha256"]), 64)

            eta_focus = eta[:, 1:4, 1:4]
            expected_eta_focus = np.full((3, 3), np.nan, dtype=np.float64)
            expected_eta_focus[shared_mask] = np.mean(
                eta_focus[:, shared_mask],
                axis=0,
                dtype=np.float64,
            )
            for level in metadata["levels"]:
                level_dir = result / level["relative_path"]
                level_metadata = json.loads(
                    (level_dir / "metadata.json").read_text(encoding="utf-8")
                )
                mean_eta = np.load(
                    level_dir / "mean_eta.npy",
                    allow_pickle=False,
                )
                np.testing.assert_allclose(
                    mean_eta,
                    expected_eta_focus,
                    rtol=0.0,
                    atol=1e-7,
                    equal_nan=True,
                )
                self.assertIn(
                    "unique free-surface elevation",
                    level_metadata["eta_semantics"],
                )
                self.assertEqual(
                    level_metadata["time_window"]["retained_time_count"],
                    48,
                )
                self.assertEqual(level_metadata["figures"], [])
                self.assertFalse((level_dir / "figures").exists())
                self.assertEqual(
                    level_metadata["mask_and_missing_data"][
                        "shared_common_ocean_cell_count"
                    ],
                    8,
                )
                self.assertTrue(
                    np.all(
                        np.isfinite(
                            np.load(
                                level_dir / "pc.npy",
                                allow_pickle=False,
                            )
                        )
                    )
                )

    def test_replot_adds_latest_scale_assets_and_shared_coverage_qc(self) -> None:
        """重繪應補齊內嵌／透明比例尺與四層 QC 圖，且不得修改來源 family。

        合成 family 的表層、-5 m 與 -10 m 都有 9 格達標，只有 -20 m 因一格水深不足
        剩 8 格。coverage metadata 必須逐層保存這個差異；四個圖目錄則都應使用既有
        8 格共同遮罩，並提供可直接放報告的 `_with_vector_scale` 與透明後製素材。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coastline_path = root / "land.geojson"
            _write_json(
                coastline_path,
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            # 測試 polygon 放在研究框西南角的小範圍，滿足 renderer 要求
                            # bbox 內必須有岸線；coverage 計數直接來自陣列，不受圖層遮蔽。
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [120.010, 24.010],
                                        [120.012, 24.010],
                                        [120.012, 24.012],
                                        [120.010, 24.012],
                                        [120.010, 24.010],
                                    ]
                                ],
                            },
                        }
                    ],
                },
            )
            config_path = _make_fixed_depth_config(
                root,
                hashlib.sha256(coastline_path.read_bytes()).hexdigest(),
            )
            surface_root, native_root, _ = _make_paired_cache(root)
            source_run = run_fixed_depth_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "derived",
            )
            source_metadata_path = source_run / "metadata.json"
            source_metadata_sha256 = hashlib.sha256(
                source_metadata_path.read_bytes()
            ).hexdigest()

            bundle = replot_fixed_depth_multivariate_svd(
                run_dir=source_run,
                output_root=root / "derived",
                config_path=config_path,
            )
            metadata = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            coverage = metadata["coverage_qc"]
            self.assertEqual(bundle.name, "academic_report_ready_v6")
            self.assertEqual(
                metadata["schema_name"],
                "ocm_fixed_depth_svd_figure_bundle",
            )
            self.assertFalse(metadata["source_run"]["native_cache_read"])
            self.assertFalse(metadata["source_run"]["surface_cache_read"])
            self.assertEqual(
                metadata["source_run"]["science_provenance_sha256"],
                json.loads(source_metadata_path.read_text(encoding="utf-8"))[
                    "science_provenance_sha256"
                ],
            )
            self.assertFalse(
                metadata["source_run"]["vertical_interpolation_called"]
            )
            self.assertFalse(metadata["source_run"]["svd_solver_called"])
            self.assertEqual(coverage["analysis_geometry_cell_count"], 9)
            self.assertEqual(coverage["shared_common_cell_count"], 8)
            self.assertEqual(coverage["shared_excluded_cell_count"], 1)
            self.assertTrue(
                any("共同 8 格遮罩" in item for item in metadata["limitations"])
            )
            self.assertEqual(
                [
                    panel["threshold_pass_cell_count"]
                    for panel in coverage["panels"]
                ],
                [9, 9, 9, 8],
            )
            self.assertTrue(
                (
                    bundle
                    / "figures"
                    / "report"
                    / "fixed_depth_shared_coverage_qc_report.png"
                ).is_file()
            )
            for level in metadata["levels"]:
                report_dir = (
                    bundle
                    / level["relative_path"]
                    / "figures"
                    / "report"
                )
                self.assertTrue(
                    (
                        report_dir
                        / "svd_mode_01_spatial_report_with_vector_scale.png"
                    ).is_file()
                )
                self.assertTrue(
                    (
                        report_dir
                        / "svd_mode_01_spatial_report_vector_scale_transparent.png"
                    ).is_file()
                )
                self.assertFalse(
                    (
                        report_dir
                        / "svd_mode_01_spatial_report_vector_scale.png"
                    ).exists()
                )
                # 固定深度圖雖共用同一份海面高度 η，標題仍必須逐圖標出速度層位，
                # 否則四層報告圖脫離目錄後無法辨識其 u/v 代表哪個物理深度。
                spatial_svg = (
                    report_dir / "svd_mode_01_spatial_report_with_vector_scale.svg"
                ).read_text(encoding="utf-8")
                self.assertIn(level["velocity_context_zh"], spatial_svg)
            self.assertEqual(
                hashlib.sha256(source_metadata_path.read_bytes()).hexdigest(),
                source_metadata_sha256,
            )
            self.assertFalse((source_run / "figures").exists())
            with self.assertRaisesRegex(FileExistsError, "提升 figures.style"):
                replot_fixed_depth_multivariate_svd(
                    run_dir=source_run,
                    output_root=root / "derived",
                    config_path=config_path,
                )


if __name__ == "__main__":
    unittest.main()
