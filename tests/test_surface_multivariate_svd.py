"""表層 u/v/eta SVD 的合成資料測試。

測試刻意建立 schema 3.0 格式的小型 `ocm_surface` 快取，而非讀取任何 raw NetCDF。它驗證
focus 切片、三變數共同遮罩、短缺值插補、面積加權 SVD 與 derived output 契約，讓
明日 SERVER 的貢寮 pilot 在真實資料前先有可重現的數值基準。
"""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ocm_svd_analysis.batch import load_batch_config, run_surface_multivariate_svd_batch  # noqa: E402
from ocm_svd_analysis.replot import replot_surface_multivariate_svd  # noqa: E402
from ocm_svd_analysis.replot_batch import replot_surface_multivariate_svd_batch  # noqa: E402
from ocm_svd_analysis.surface_multivariate_svd import load_analysis_config, run_surface_multivariate_svd  # noqa: E402


def write_json(path: Path, payload: dict[str, object]) -> None:
    """寫入測試專用 JSON；所有內容位於 TemporaryDirectory，不會建立正式分析資料。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_config(
    path: Path,
    *,
    analysis_label: str = "synthetic_surface_svd",
    analysis_unit_id: str = "synthetic_surface_svd_aoi_v1",
    years: tuple[int, ...] | None = None,
    known_time_axis_repairs: list[dict[str, object]] | None = None,
    maximum_source_gap_hours: float | None = 2.0,
) -> None:
    """建立最小但完整的三變數 SVD 設定，驗證 3×3 AOI 與五個輸出模態。

    bbox 外的 memory-map 讀取緩衝仍會保留在輸出格網，藉此測試新的
    `analysis_bbox_cell_center` 政策確實只讓中間 3×3 cell center 進入共同有效遮罩，而
    不會因讀取小窗外擴把外圈錯放入 SVD。另建立一個只覆蓋左上角的合成 GeoJSON
    陸地 polygon，確認正式圖會加入海岸線，但不會把圖面陸地寫回科學遮罩。
    `maximum_source_gap_hours=None` 用於驗證已核定的全部可得樣本可解除長缺口拒絕，
    但實際斷點仍必須寫入 metadata 供研究報告揭露。
    """

    coastline_path = path.parent / "synthetic_land.geojson"
    write_json(
        coastline_path,
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"source": "synthetic coastline for renderer contract"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [120.005, 22.025],
                                [120.020, 22.025],
                                [120.020, 22.040],
                                [120.005, 22.040],
                                [120.005, 22.025],
                            ]
                        ],
                    },
                }
            ],
        },
    )
    coastline_sha256 = hashlib.sha256(coastline_path.read_bytes()).hexdigest()
    write_json(
        path,
        {
            "schema_version": "1.0.0",
            "analysis_kind": "surface_multivariate_svd",
            "analysis_label": analysis_label,
            "focus": {
                "focus_id": "synthetic_focus",
                "name_zh": "合成 SVD 研究區",
                "approval_status": "approved",
                "flow_domain_id": "synthetic_domain",
                "bbox_lon_lat": [120.005, 120.035, 22.005, 22.035],
                "bbox_order": "lon_min, lon_max, lat_min, lat_max",
                "anchor_lonlat": [120.02, 22.02],
                "analysis_unit_id": analysis_unit_id,
                "source_analysis_units_config": "synthetic/analysis_units.json",
                "source_analysis_units_config_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "spatial_mask_policy": "analysis_bbox_cell_center",
            },
            "input": {
                **({"year": 2025} if years is None else {"years": list(years)}),
                "months": [1, 2],
                "required_cache_schema_major": 3,
                "required_status": "ready",
                "required_cache_kinds": ["standard_month"],
                "expected_timestep_hours": 1.0,
                "maximum_source_gap_hours": maximum_source_gap_hours,
                **({"known_time_axis_repairs": known_time_axis_repairs} if known_time_axis_repairs is not None else {}),
            },
            "mask_and_missing_data": {
                "static_ocean_mask": "mask_static.npy",
                "minimum_static_ocean_cells": 5,
                "minimum_cell_triplet_valid_fraction": 0.95,
                "max_consecutive_missing_timesteps_to_interpolate": 2,
                "minimum_retained_time_fraction": 0.95,
            },
            "svd": {
                "variables": ["u_surface_mps", "v_surface_mps", "eta_m"],
                "anomaly_reference": "synthetic per-cell time mean",
                "normalization": "synthetic vector RMS plus eta RMS",
                "spatial_weight": "sqrt(cell_area_m2), repeated for u, v, eta",
                "requested_mode_count": 5,
                "minimum_reported_mode_count": 5,
                "sign_convention": "synthetic anchor convention",
            },
            "parallel_execution": {
                "io_workers": 2,
                "linear_algebra_threads": 2,
            },
            "figures": {
                # 正式測試只交付可直接報告的白底完整標示圖；這裡刻意不提供透明背景
                # 選項，確保設定契約本身不可能重新啟用缺少標題、單位與圖例的舊素材。
                "style": "academic_report_ready_v6",
                "mode_count": 5,
                "max_quiver_arrows_per_axis": 8,
                # SVG 會保留圖中文字，測試可直接檢查正式標題只使用 SVD；PNG 則用於
                # alpha channel 驗證。兩種格式一起測，才能涵蓋正式六區交付契約。
                "output_formats": ["png", "svg"],
                "raster_dpi": 90,
                # 測試使用 config 同目錄的相對路徑，驗證 resolver 不依賴開發機絕對路徑；
                # SHA-256 則確保換檔後不會沿用同一個 figure provenance。
                "coastline_geojson": coastline_path.name,
                "coastline_geojson_sha256": coastline_sha256,
            },
        },
    )


def make_surface_cache(
    root: Path,
    *,
    years: tuple[int, ...] = (2025,),
    second_month_time_shift_hours: int = 0,
) -> Path:
    """建立含單一短缺值的兩個 5×5、各 24 小時 surface cache。

    各分量由固定 seed 的隨機值組成，以確保 3×3 focus 的 27 維狀態具有至少五個非零
    SVD 模態。time index 7 的一格 u 缺值用來確認三分量聯合遮罩與兩步內插補會保留
    全部時次，並在輸出 imputed mask 中留下證據。`years` 可建立多年度標籤的連續合成
    時軸，確認平行年／月 I/O 仍會依設定年份再月序串接，不能因 worker 完成先後改變
    SVD 時間軸。`second_month_time_shift_hours` 只供時間修正測試製造一段已知錯標，
    不會改動任何流場值，因此可確認修正只作用在 UTC 座標。
    """

    domain_root = root / "ocm_surface" / "synthetic_domain"
    grid_dir = domain_root / "grid"
    grid_dir.mkdir(parents=True)
    lon = np.array([120.00, 120.01, 120.02, 120.03, 120.04], dtype=np.float64)
    lat = np.array([22.00, 22.01, 22.02, 22.03, 22.04], dtype=np.float64)
    area = np.full((5, 5), 1_000_000.0, dtype=np.float64)
    static = np.ones((5, 5), dtype=bool)
    for filename, array in (("lon.npy", lon), ("lat.npy", lat), ("cell_area_m2.npy", area), ("mask_static.npy", static)):
        np.save(grid_dir / filename, array, allow_pickle=False)
    write_json(grid_dir / "metadata.json", {"cache_schema_version": "3.0.0", "domain": {"domain_id": "synthetic_domain"}})

    random = np.random.default_rng(20260728)
    time_count = 24
    base_time = np.datetime64("2025-01-01T00:00:00", "ns").astype(np.int64)
    for month_index, (year, month) in enumerate((year, month) for year in years for month in (1, 2)):
        month_id = f"{year}{month:02d}"
        month_dir = domain_root / "months" / month_id
        month_dir.mkdir(parents=True)
        time = base_time + (month_index * time_count + np.arange(time_count, dtype=np.int64)) * 3_600_000_000_000
        if month_index == 1 and second_month_time_shift_hours:
            time = time + second_month_time_shift_hours * 3_600_000_000_000
        u = (0.2 + random.normal(size=(time_count, 5, 5))).astype(np.float32)
        v = (-0.1 + random.normal(size=(time_count, 5, 5))).astype(np.float32)
        eta = (0.05 + random.normal(size=(time_count, 5, 5))).astype(np.float32)
        valid = np.ones((time_count, 5, 5), dtype=bool)
        if month_index == 0:
            u[7, 2, 2] = np.nan
        for filename, array in (("time_utc_ns.npy", time), ("u_surface_mps.npy", u), ("v_surface_mps.npy", v), ("eta_m.npy", eta), ("valid_mask_surface.npy", valid)):
            np.save(month_dir / filename, array, allow_pickle=False)
        write_json(
            month_dir / "metadata.json",
            {
                "cache_schema_version": "3.0.0",
                "status": "ready",
                "cache_kind": "standard_month",
                "month": month_id,
                "domain": {"domain_id": "synthetic_domain"},
            },
        )
    return root / "ocm_surface"


class SurfaceMultivariateSvdTest(unittest.TestCase):
    """確認 SVD 數值輸出遵守三變數、遮罩與可追溯性契約。"""

    def test_run_writes_expected_arrays_and_records_short_gap_interpolation(self) -> None:
        """一個短 u 缺值應聯合插補三分量且不縮短兩月共 48 個可用時間樣本。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            make_config(config_path)
            surface_root = make_surface_cache(root)
            result_dir = run_surface_multivariate_svd(config_path=config_path, surface_root=surface_root, output_root=root / "derived", make_figures=True)

            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            mode_u = np.load(result_dir / "mode_u.npy", allow_pickle=False)
            mode_v = np.load(result_dir / "mode_v.npy", allow_pickle=False)
            mode_eta = np.load(result_dir / "mode_eta.npy", allow_pickle=False)
            pc = np.load(result_dir / "pc.npy", allow_pickle=False)
            pc_standardized = np.load(result_dir / "pc_standardized.npy", allow_pickle=False)
            regression_u = np.load(result_dir / "regression_u.npy", allow_pickle=False)
            imputed = np.load(result_dir / "imputed_mask.npy", allow_pickle=False)
            explained = np.load(result_dir / "explained_variance.npy", allow_pickle=False)
            geometry = np.load(result_dir / "analysis_geometry_mask.npy", allow_pickle=False)
            valid = np.load(result_dir / "valid_mask.npy", allow_pickle=False)
            plot_metadata = json.loads((result_dir / "figures" / "plot_metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(metadata["status"], "analysis_ready")
            self.assertEqual(metadata["time_window"]["retained_time_count"], 48)
            self.assertEqual(metadata["mask_and_missing_data"]["imputed_scalar_count"], 3)
            self.assertEqual(mode_u.shape, (5, 5, 5))
            self.assertEqual(mode_v.shape, mode_u.shape)
            self.assertEqual(mode_eta.shape, mode_u.shape)
            self.assertEqual(pc.shape, (5, 48))
            self.assertEqual(pc_standardized.shape, pc.shape)
            self.assertEqual(regression_u.shape, mode_u.shape)
            np.testing.assert_allclose(np.mean(pc_standardized, axis=1), 0.0, atol=1e-12)
            np.testing.assert_allclose(np.std(pc_standardized, axis=1, ddof=1), 1.0, atol=1e-12)
            np.testing.assert_allclose(
                regression_u[:, None, :, :] * pc_standardized[:, :, None, None],
                mode_u[:, None, :, :] * pc[:, :, None, None],
                rtol=1e-12,
                atol=1e-12,
            )
            self.assertEqual(imputed.shape, (48, 3, 5, 5))
            self.assertEqual(int(np.count_nonzero(imputed)), 3)
            self.assertEqual(int(np.count_nonzero(geometry)), 9)
            self.assertEqual(int(np.count_nonzero(valid)), 9)
            self.assertTrue(np.all(np.isnan(mode_u[:, ~geometry])))
            self.assertEqual(metadata["analysis_unit"]["geometry_inclusion"], "closed_bbox_cell_center")
            self.assertEqual(metadata["analysis_unit"]["geometry_cell_center_count_before_static_mask"], 9)
            self.assertTrue(np.all(np.isfinite(pc)))
            self.assertTrue(np.all(explained > 0))
            self.assertLess(metadata["quality_checks"]["spatial_vector_orthogonality_max_abs_error"], 1e-10)
            self.assertLess(metadata["quality_checks"]["full_rank_relative_reconstruction_error"], 1e-10)
            self.assertEqual(metadata["parallel_execution"]["io_workers_used"], 2)
            self.assertGreaterEqual(metadata["parallel_execution"]["linear_algebra_threads_used"], 1)
            self.assertLessEqual(metadata["parallel_execution"]["linear_algebra_threads_used"], 2)
            expected_performance_stages = {
                "configuration_and_output_validation",
                "surface_focus_month_io",
                "mask_and_missing_data_preparation",
                "svd_solver",
                "physical_and_visualization_field_derivation",
                "array_and_provenance_serialization",
                "figure_rendering",
            }
            self.assertEqual(set(metadata["performance"]["stages_seconds"]), expected_performance_stages)
            self.assertTrue(all(value >= 0.0 for value in metadata["performance"]["stages_seconds"].values()))
            self.assertGreaterEqual(
                metadata["performance"]["total_seconds"],
                metadata["performance"]["measured_stage_sum_seconds"],
            )
            self.assertEqual(plot_metadata["style"], "academic_report_ready_v6")
            self.assertEqual(plot_metadata["schema_version"], "6.3.0")
            self.assertTrue(plot_metadata["text_policy"]["assets_contain_text"])
            self.assertEqual(plot_metadata["rendering"]["report_background"], "opaque white")
            self.assertEqual(
                plot_metadata["geographic_context"]["sha256"],
                hashlib.sha256((root / "synthetic_land.geojson").read_bytes()).hexdigest(),
            )
            self.assertEqual(plot_metadata["geographic_context"]["plotted_polygon_count"], 1)
            self.assertIn("does not alter SVD", plot_metadata["geographic_context"]["semantics"])
            self.assertEqual(plot_metadata["time_series"]["gap_break_count"], 0)
            self.assertEqual(len(plot_metadata["assets"]["modes"]), 5)
            # 平均場與五個模態各有透明獨立參考尺，以及內嵌參考尺備用主圖的
            # PNG/SVG，因此交付檔維持 50；兩種資產都必須與標準主圖共用 stem，
            # 六區批次後製才不會配錯物理尺度。
            self.assertEqual(len(metadata["figures"]), 50)
            self.assertTrue(all((result_dir / figure).is_file() for figure in metadata["figures"]))
            self.assertTrue((result_dir / "figures" / "REPORT_GUIDE.md").is_file())
            mode_one_assets = plot_metadata["assets"]["modes"][0]
            self.assertEqual(
                mode_one_assets["vector_scale_transparent_report_files"],
                [
                    "figures/report/svd_mode_01_spatial_report_vector_scale_transparent.png",
                    "figures/report/svd_mode_01_spatial_report_vector_scale_transparent.svg",
                ],
            )
            self.assertEqual(
                mode_one_assets["with_vector_scale_report_files"],
                [
                    "figures/report/svd_mode_01_spatial_report_with_vector_scale.png",
                    "figures/report/svd_mode_01_spatial_report_with_vector_scale.svg",
                ],
            )
            plotted_bbox = plot_metadata["rendering"]["plotted_valid_cell_edge_bbox_lon_lat"]
            longitude_ticks = plot_metadata["rendering"]["longitude_axis_ticks_degrees_east"]
            latitude_ticks = plot_metadata["rendering"]["latitude_axis_ticks_degrees_north"]
            self.assertAlmostEqual(longitude_ticks[0], plotted_bbox[0])
            self.assertAlmostEqual(longitude_ticks[-1], plotted_bbox[1])
            self.assertAlmostEqual(latitude_ticks[0], plotted_bbox[2])
            self.assertAlmostEqual(latitude_ticks[-1], plotted_bbox[3])
            colorbar_ticks = mode_one_assets["eta_colorbar_ticks_m_per_pc_standard_deviation"]
            eta_limit = mode_one_assets["eta_symmetric_color_limit_m_per_pc_standard_deviation"]
            self.assertAlmostEqual(colorbar_ticks[0], -eta_limit)
            self.assertAlmostEqual(colorbar_ticks[-1], eta_limit)
            # 以拆字方式建立舊標籤，避免測試原始碼本身也被全專案稽核誤認為可用介面。
            forbidden_unlabeled_asset_term = "cl" + "ean"
            self.assertNotIn(forbidden_unlabeled_asset_term, json.dumps(plot_metadata).lower())
            self.assertFalse(
                any(forbidden_unlabeled_asset_term in figure.lower() for figure in metadata["figures"])
            )
            forbidden_alias = "E" + "OF"
            report_guide = (result_dir / "figures" / "REPORT_GUIDE.md").read_text(encoding="utf-8")
            report_svg = (
                result_dir / "figures" / "report" / "svd_mode_01_spatial_report.svg"
            ).read_text(encoding="utf-8")
            vector_scale_transparent_svg = (
                result_dir
                / "figures"
                / "report"
                / "svd_mode_01_spatial_report_vector_scale_transparent.svg"
            ).read_text(encoding="utf-8")
            with_vector_scale_svg = (
                result_dir
                / "figures"
                / "report"
                / "svd_mode_01_spatial_report_with_vector_scale.svg"
            ).read_text(encoding="utf-8")
            self.assertNotIn(forbidden_alias, report_guide)
            self.assertNotIn(forbidden_alias, report_svg)
            self.assertIn("SVD 模態 1", report_svg)
            self.assertIn("解釋變異量", report_svg)
            self.assertNotIn("EV=", report_svg)
            self.assertNotIn("focus anchor", report_svg.lower())
            self.assertIn("#d9d6cf", report_svg.lower())
            formatted_vector_reference = (
                f"{mode_one_assets['vector_reference_mps_per_pc_standard_deviation_at_95th_percentile']:.2f}"
            )
            self.assertNotIn(formatted_vector_reference + " m/s", report_svg)
            self.assertIn(formatted_vector_reference + " m/s", vector_scale_transparent_svg)
            self.assertIn(formatted_vector_reference + " m/s", with_vector_scale_svg)
            # 透明獨立版只允許純黑內容與透明畫布，不得殘留前一版白色底板或 halo。
            # Matplotlib 透明 figure 的 patch 會以 `fill: none` 表示，實際 alpha 契約
            # 另由下方 PNG 像素測試確認。
            self.assertNotIn("#ffffff", vector_scale_transparent_svg.lower())
            self.assertIn("#000000", vector_scale_transparent_svg.lower())

            # 報告版 PNG 即使在深色圖片檢視器或簡報母片上也必須維持白底；若 PNG 含
            # alpha，所有像素亦須完全不透明，避免再次出現「圖底色變黑」的合成問題。
            import matplotlib.image as mpimg

            report_image = mpimg.imread(result_dir / "figures" / "report" / "svd_mode_01_spatial_report.png")
            if report_image.ndim == 3 and report_image.shape[2] == 4:
                self.assertTrue(np.allclose(report_image[:, :, 3], 1.0))
            vector_scale_transparent_image = mpimg.imread(
                result_dir
                / "figures"
                / "report"
                / "svd_mode_01_spatial_report_vector_scale_transparent.png"
            )
            self.assertEqual(vector_scale_transparent_image.ndim, 3)
            self.assertEqual(vector_scale_transparent_image.shape[2], 4)
            transparent_alpha = vector_scale_transparent_image[:, :, 3]
            # 四角與大部分畫布應完全透明，而純黑箭頭／文字像素必須可見。限制不鎖死
            # antialiasing 的精確 alpha，只驗證「無白底」與「內容存在」兩個語意。
            self.assertTrue(np.allclose(transparent_alpha[[0, -1]][:, [0, -1]], 0.0))
            self.assertGreater(float(np.max(transparent_alpha)), 0.99)
            # 緊密裁切會刻意降低透明像素比例；仍要求過半畫布完全透明，以確認沒有
            # 矩形底板，同時不把 85% 這類舊大畫布門檻誤當成品質目標。
            self.assertGreater(float(np.mean(transparent_alpha == 0.0)), 0.60)
            nonzero_rows, nonzero_columns = np.where(transparent_alpha > 0.0)
            left_padding = int(nonzero_columns.min())
            right_padding = int(transparent_alpha.shape[1] - 1 - nonzero_columns.max())
            top_padding = int(nonzero_rows.min())
            bottom_padding = int(transparent_alpha.shape[0] - 1 - nonzero_rows.max())
            # artist bbox 四周使用同一個 inch padding；容許 2 px 是因 PNG antialiasing
            # 與浮點 bbox 轉整數像素時可能各自向內／向外取整一個像素。
            self.assertLessEqual(abs(left_padding - right_padding), 2)
            self.assertLessEqual(abs(top_padding - bottom_padding), 2)
            with_vector_scale_image = mpimg.imread(
                result_dir
                / "figures"
                / "report"
                / "svd_mode_01_spatial_report_with_vector_scale.png"
            )
            if with_vector_scale_image.ndim == 3 and with_vector_scale_image.shape[2] == 4:
                self.assertTrue(np.allclose(with_vector_scale_image[:, :, 3], 1.0))
            # 緊密裁切後的透明素材應落在主圖寬度 10–18%、高度不超過 4%；下限防止
            # 文字被誤裁，上限防止回到 601×154 且左右大量留白的舊版素材。
            self.assertGreaterEqual(
                vector_scale_transparent_image.shape[1] / report_image.shape[1],
                0.10,
            )
            self.assertLessEqual(
                vector_scale_transparent_image.shape[1] / report_image.shape[1],
                0.18,
            )
            self.assertLessEqual(
                vector_scale_transparent_image.shape[0] / report_image.shape[0],
                0.04,
            )
            # 內嵌備用圖必須和標準主圖維持相同像素尺寸，PowerPoint 替換時不應跳動。
            self.assertEqual(with_vector_scale_image.shape, report_image.shape)

    def test_existing_run_is_never_silently_overwritten(self) -> None:
        """同一輸入與設定應得到同一 run ID，第二次執行必須拒絕覆寫既有科學成果。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            make_config(config_path)
            surface_root = make_surface_cache(root)
            output_root = root / "derived"
            run_surface_multivariate_svd(config_path=config_path, surface_root=surface_root, output_root=output_root, make_figures=False)
            with self.assertRaises(FileExistsError):
                run_surface_multivariate_svd(config_path=config_path, surface_root=surface_root, output_root=output_root, make_figures=False)

    def test_replot_uses_existing_arrays_without_modifying_or_recomputing_source_run(self) -> None:
        """重繪必須產生獨立 immutable bundle，且來源 run 的任何位元都不得改變。

        合成科學 run 刻意不產生圖；重繪器只能從已發布的回歸模態與標準化 PC 補出圖面。
        測試以 metadata SHA-256 和來源 `figures/` 不存在雙重確認沒有回寫來源，並驗證
        bundle 明載未讀 surface cache、未呼叫 SVD solver。若改動 bbox 等科學設定，也
        必須在畫圖前拒絕，避免六區報告把既有陣列貼上錯誤地理標籤。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            make_config(config_path)
            source_run = run_surface_multivariate_svd(
                config_path=config_path,
                surface_root=make_surface_cache(root),
                output_root=root / "derived",
                make_figures=False,
            )
            source_metadata_path = source_run / "metadata.json"
            source_metadata_sha256 = hashlib.sha256(source_metadata_path.read_bytes()).hexdigest()
            self.assertFalse((source_run / "figures").exists())

            bundle = replot_surface_multivariate_svd(
                run_dir=source_run,
                output_root=root / "derived",
                config_path=config_path,
            )
            bundle_metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(bundle.is_dir())
            self.assertEqual(bundle.name, "academic_report_ready_v6")
            self.assertEqual(bundle_metadata["bundle_id"], "academic_report_ready_v6")
            self.assertEqual(bundle_metadata["schema_version"], "1.1.0")
            bundle_provenance_sha256 = bundle_metadata["bundle_provenance_sha256"]
            self.assertEqual(len(bundle_provenance_sha256), 64)
            self.assertTrue(
                all(character in "0123456789abcdef" for character in bundle_provenance_sha256)
            )
            self.assertEqual(bundle_metadata["status"], "figures_ready")
            self.assertEqual(bundle_metadata["source_run"]["run_id"], source_run.name)
            self.assertEqual(bundle_metadata["source_run"]["metadata_sha256"], source_metadata_sha256)
            self.assertFalse(bundle_metadata["source_run"]["surface_cache_read"])
            self.assertFalse(bundle_metadata["source_run"]["svd_solver_called"])
            self.assertEqual(len(bundle_metadata["figures"]), 50)
            self.assertTrue(all((bundle / relative_path).is_file() for relative_path in bundle_metadata["figures"]))
            self.assertEqual(
                set(bundle_metadata["performance"]["stages_seconds"]),
                {
                    "source_and_configuration_validation",
                    "existing_run_array_loading",
                    "figure_rendering",
                    "bundle_provenance_serialization",
                },
            )
            self.assertEqual(hashlib.sha256(source_metadata_path.read_bytes()).hexdigest(), source_metadata_sha256)
            self.assertFalse((source_run / "figures").exists())
            # 對外目錄只保留 style 版本，同一 v6 不得靠另一個 hash 目錄並存；若繪圖
            # 規格改變，錯誤訊息必須明確要求升版，避免六區成果樹再次累積難辨識版本。
            with self.assertRaisesRegex(FileExistsError, "提升 figures.style"):
                replot_surface_multivariate_svd(
                    run_dir=source_run,
                    output_root=root / "derived",
                    config_path=config_path,
                )

            mismatched_config = json.loads(config_path.read_text(encoding="utf-8"))
            mismatched_config["focus"]["bbox_lon_lat"][0] += 0.001
            mismatched_config_path = root / "mismatched_config.json"
            write_json(mismatched_config_path, mismatched_config)
            with self.assertRaisesRegex(ValueError, "科學設定變更"):
                replot_surface_multivariate_svd(
                    run_dir=source_run,
                    output_root=root / "derived",
                    config_path=mismatched_config_path,
                )

    def test_two_year_input_uses_2024_then_2025_month_order(self) -> None:
        """`input.years` 必須讀取 2024+2025 全部月份，並保留設定的年度、月序排序。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "two_year_config.json"
            make_config(config_path, analysis_label="synthetic_two_year", analysis_unit_id="synthetic_two_year_aoi_v1", years=(2024, 2025))
            result_dir = run_surface_multivariate_svd(
                config_path=config_path,
                surface_root=make_surface_cache(root, years=(2024, 2025)),
                output_root=root / "derived",
                make_figures=False,
            )
            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["time_window"]["initial_time_count"], 96)
            self.assertEqual(
                [source["month"] for source in metadata["input_surface"]["source_months"]],
                ["202401", "202402", "202501", "202502"],
            )
            self.assertEqual(metadata["parallel_execution"]["io_workers_used"], 2)

    def test_available_samples_config_can_report_but_not_limit_a_long_source_gap(self) -> None:
        """全部可得設定以 null 解除上限，但必須保留可稽核的實際來源斷點。

        第二個合成月刻意往後平移 24 小時，使月界從逐時資料變成 25 小時缺口。這模擬
        `standard_partial_month` 的來源不連續：嚴格設定應拒絕，研究團隊明確核定的
        `maximum_source_gap_hours=null` 則可完成 SVD，且 metadata 必須留下最大缺口與斷點數，
        不能因解除拒絕條件而遺失報告所需的資料品質證據。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "available_samples_config.json"
            make_config(
                config_path,
                analysis_label="synthetic_available_samples",
                maximum_source_gap_hours=None,
            )
            result_dir = run_surface_multivariate_svd(
                config_path=config_path,
                surface_root=make_surface_cache(root, second_month_time_shift_hours=24),
                output_root=root / "derived",
                make_figures=False,
            )
            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            source_axis = metadata["input_surface"]["source_time_axis"]
            self.assertIsNone(source_axis["maximum_gap_limit_hours"])
            self.assertEqual(source_axis["maximum_gap_policy"], "unbounded_but_reported")
            self.assertEqual(source_axis["maximum_gap_hours"], 25.0)
            self.assertEqual(source_axis["gap_break_count"], 1)

    def test_known_time_axis_repair_requires_expected_original_bounds_and_preserves_samples(self) -> None:
        """已知月界錯標只能在原始 UTC 完全吻合時平移，且不得刪除或複製流場樣本。

        合成第二月的 24 筆資料刻意往前錯標 24 小時，與第一月完全重疊；設定再以鎖定
        起訖時間的顯式規則移回正確位置。成果應保留 48 筆，並在 metadata 記錄 24 筆
        時間座標修正。若原始起點不符，流程必須停止而不是猜測。
        """

        repair = {
            "month": "202502",
            "start_index": 0,
            "stop_index": 24,
            "shift_hours": 24.0,
            "expected_original_start_utc": "2025-01-01T00:00:00Z",
            "expected_original_end_utc": "2025-01-01T23:00:00Z",
            "reason": "合成來源第二月整段往前錯標一天；只供驗證顯式時間座標修正。",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "repair_config.json"
            make_config(config_path, analysis_label="synthetic_time_repair", known_time_axis_repairs=[repair])
            result_dir = run_surface_multivariate_svd(
                config_path=config_path,
                surface_root=make_surface_cache(root, second_month_time_shift_hours=-24),
                output_root=root / "derived",
                make_figures=False,
            )
            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            repaired_time = np.load(result_dir / "time_utc_ns.npy", allow_pickle=False)
            self.assertEqual(metadata["time_window"]["initial_time_count"], 48)
            self.assertEqual(metadata["input_surface"]["repaired_time_step_count"], 24)
            self.assertEqual(len(metadata["input_surface"]["known_time_axis_repairs"]), 1)
            self.assertTrue(np.all(np.diff(repaired_time) == 3_600_000_000_000))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bad_config_path = root / "bad_repair_config.json"
            bad_repair = dict(repair)
            bad_repair["expected_original_start_utc"] = "2025-01-02T00:00:00Z"
            bad_repair["expected_original_end_utc"] = "2025-01-02T23:00:00Z"
            make_config(bad_config_path, analysis_label="synthetic_bad_time_repair", known_time_axis_repairs=[bad_repair])
            with self.assertRaisesRegex(ValueError, "原始起訖 UTC 不符"):
                run_surface_multivariate_svd(
                    config_path=bad_config_path,
                    surface_root=make_surface_cache(root, second_month_time_shift_hours=-24),
                    output_root=root / "derived",
                    make_figures=False,
                )

    def test_six_region_batch_config_reserves_24_blas_cores_on_32_core_server(self) -> None:
        """六區 batch 必須把六個上游分析單元與 6×4=24 核受控配置鎖在同一份設定。"""

        batch_config = load_batch_config(PROJECT_ROOT / "configs" / "six_regions_surface_svd_2025_batch.json")
        self.assertEqual(batch_config.server_minimum_cpu_cores, 32)
        self.assertEqual(len(batch_config.regions), 6)
        self.assertEqual(batch_config.max_concurrent_regions, 6)
        self.assertEqual(batch_config.per_region_linear_algebra_threads, 4)
        self.assertEqual(batch_config.max_concurrent_regions * batch_config.per_region_linear_algebra_threads, 24)
        self.assertEqual(
            {region.analysis_unit_id for region in batch_config.regions},
            {
                "guishan_surface_svd_candidate_v3",
                "gongliao_surface_svd_candidate_v3",
                "hsinchu_surface_svd_candidate_v3",
                "houwan_nmmba_surface_svd_candidate_v3",
                "beigan_surface_svd_aoi_v1",
                "nangan_surface_svd_aoi_v1",
            },
        )

    def test_six_svd_configs_match_preprocessing_analysis_unit_contract(self) -> None:
        """SVD 專案六份設定須逐欄對齊前處理專案的唯一分析單元版本與檔案雜湊。

        這是跨專案契約測試：若某一端私自改 bbox、anchor、domain 或核定狀態，即使 JSON
        本身合法也必須在本機 CI 被攔下，避免 SERVER 產出地理範圍不一致的 SVD 成果。
        """

        preprocessing_config = PROJECT_ROOT.parent / "OCM-Data-Preprocessing" / "configs" / "ocm_svd_analysis_units_v1.json"
        source_bytes = preprocessing_config.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_payload = json.loads(source_bytes.decode("utf-8"))
        upstream_units = {unit["analysis_unit_id"]: unit for unit in source_payload["analysis_units"]}
        batch = load_batch_config(PROJECT_ROOT / "configs" / "six_regions_surface_svd_2025_batch.json")
        self.assertEqual(batch.source_analysis_units_config_sha256, source_hash)
        for region in batch.regions:
            downstream = load_analysis_config(region.config_path)
            upstream = upstream_units[region.analysis_unit_id]
            self.assertEqual(downstream.source_analysis_units_config_sha256, source_hash)
            self.assertEqual(downstream.domain_id, upstream["flow_domain_id"])
            self.assertEqual(list(downstream.bbox), upstream["analysis_bbox"])
            self.assertEqual(list(downstream.anchor_lonlat), upstream["anchor_lonlat"])
            self.assertEqual(downstream.approval_status, upstream["approval_status"])
            self.assertEqual(downstream.spatial_mask_policy, "analysis_bbox_cell_center")

    def test_parallel_batch_runs_two_independent_regions_without_sharing_outputs(self) -> None:
        """批次協調器須平行產生兩個 immutable run，且回傳設定順序的成果。

        正式 SERVER 使用 process；受限測試 sandbox 若禁止 semaphore，協調器會明確標記
        thread fallback，仍可驗證兩區輸出不共用暫存目錄或成果路徑。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root = make_surface_cache(root)
            make_config(root / "region_one.json", analysis_label="synthetic_region_one", analysis_unit_id="synthetic_region_one_aoi_v1")
            make_config(root / "region_two.json", analysis_label="synthetic_region_two", analysis_unit_id="synthetic_region_two_aoi_v1")
            write_json(
                root / "batch.json",
                {
                    "schema_version": "1.0.0",
                    "analysis_kind": "surface_multivariate_svd_batch",
                    "batch_label": "synthetic_two_region_batch",
                    "source_analysis_units_config": "synthetic/analysis_units.json",
                    "source_analysis_units_config_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "parallel_execution": {
                        "server_minimum_cpu_cores": 4,
                        "max_concurrent_regions": 2,
                        "per_region_linear_algebra_threads": 2,
                        "per_region_io_workers": 2,
                    },
                    "region_configs": [
                        {"analysis_unit_id": "synthetic_region_one_aoi_v1", "config": "region_one.json"},
                        {"analysis_unit_id": "synthetic_region_two_aoi_v1", "config": "region_two.json"},
                    ],
                },
            )
            result = run_surface_multivariate_svd_batch(
                batch_config_path=root / "batch.json",
                surface_root=surface_root,
                output_root=root / "derived",
                make_figures=False,
            )
            self.assertGreaterEqual(result.concurrent_regions_used, 1)
            self.assertGreater(result.total_elapsed_seconds, 0.0)
            self.assertEqual([region.analysis_unit_id for region in result.regions], ["synthetic_region_one_aoi_v1", "synthetic_region_two_aoi_v1"])
            self.assertTrue(all(region.status == "created" for region in result.regions))
            self.assertTrue(all(region.result_dir is not None and region.result_dir.is_dir() for region in result.regions))
            self.assertTrue(all(region.elapsed_seconds > 0.0 for region in result.regions))

    def test_parallel_replot_batch_creates_two_bundles_without_touching_science_runs(self) -> None:
        """多區重繪必須平行讀取各自 run，並保持來源科學目錄完全沒有 figures。

        這個兩區合成測試對應正式六區報告流程：先以不繪圖模式完成 immutable SVD，再由
        最多六個獨立 process 建立 figure bundles。成果回傳順序必須與輸入一致，方便報告
        程式依固定六區版面組裝，而不是依 worker 完成先後排列。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root = make_surface_cache(root)
            config_one = root / "replot_region_one.json"
            config_two = root / "replot_region_two.json"
            make_config(config_one, analysis_label="synthetic_replot_region_one", analysis_unit_id="synthetic_replot_region_one_aoi_v1")
            make_config(config_two, analysis_label="synthetic_replot_region_two", analysis_unit_id="synthetic_replot_region_two_aoi_v1")
            source_runs = tuple(
                run_surface_multivariate_svd(
                    config_path=config_path,
                    surface_root=surface_root,
                    output_root=root / "derived",
                    make_figures=False,
                )
                for config_path in (config_one, config_two)
            )
            source_hashes = {
                run.name: hashlib.sha256((run / "metadata.json").read_bytes()).hexdigest()
                for run in source_runs
            }

            result = replot_surface_multivariate_svd_batch(
                run_dirs=source_runs,
                output_root=root / "derived",
                max_concurrent_regions=2,
                config_paths=(config_one, config_two),
            )
            self.assertGreaterEqual(result.concurrent_regions_used, 1)
            self.assertLessEqual(result.concurrent_regions_used, 2)
            self.assertGreater(result.total_elapsed_seconds, 0.0)
            self.assertEqual([item.run_id for item in result.items], [run.name for run in source_runs])
            self.assertTrue(all(item.bundle_dir.is_dir() and item.elapsed_seconds > 0.0 for item in result.items))
            self.assertTrue(
                all(item.bundle_dir.name == "academic_report_ready_v6" for item in result.items)
            )
            for run in source_runs:
                self.assertFalse((run / "figures").exists())
                self.assertEqual(
                    hashlib.sha256((run / "metadata.json").read_bytes()).hexdigest(),
                    source_hashes[run.name],
                )


if __name__ == "__main__":
    unittest.main()
