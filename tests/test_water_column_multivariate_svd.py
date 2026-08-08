"""完整 flow domain 六層聯合 ``u/v/eta`` 直接 SVD 的合成資料測試。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ocm_svd_analysis.water_column_multivariate_svd import (
    VELOCITY_LEVEL_IDS,
    _plot_domain_name_zh,
    _solve_direct_svd,
    load_water_column_config,
    run_water_column_multivariate_svd,
)
from ocm_svd_analysis.water_column_host_layout import (
    HOST_LAYOUT_ID,
    HOST_LAYOUT_REQUIRED_SOURCE_FILENAMES,
    export_water_column_host_layout,
)
from ocm_svd_analysis.water_column_replot import (
    WATER_COLUMN_REPLOT_REQUIRED_ARRAY_FILENAMES,
    replot_water_column_multivariate_svd,
)


def _write_json(path: Path, payload: object) -> None:
    """以 UTF-8 建立合成 flow-domain 設定與月份 provenance。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    """計算小型合成設定的 SHA-256，模擬正式上游契約鎖定。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_paired_cache(root: Path) -> tuple[Path, Path, str]:
    """建立 4×4、48 小時、可包夾 0–50 m 的 paired synthetic cache。

    每個規則格由同 index source node 以權重 1 支撐，方便測試把 native 固定深度流速
    正確水平內插回 surface grid。所有層都有非退化隨機訊號，確保 20 個 requested modes
    具有足夠數值 rank；eta 仍只在 surface cache 保存一次。
    """

    surface_root = root / "ocm_surface"
    native_root = root / "ocm_native"
    domain = "synthetic_water_domain"
    surface_grid = surface_root / domain / "grid"
    native_grid = native_root / domain / "grid"
    surface_month = surface_root / domain / "months" / "202501"
    native_month = native_root / domain / "months" / "202501"
    for directory in (surface_grid, native_grid, surface_month, native_month):
        directory.mkdir(parents=True, exist_ok=True)

    lon = np.linspace(120.0, 120.03, 4, dtype=np.float64)
    lat = np.linspace(22.0, 22.03, 4, dtype=np.float64)
    area = np.full((4, 4), 1_000_000.0, dtype=np.float64)
    bathymetry = np.full((4, 4), 70.0, dtype=np.float64)
    static = np.ones((4, 4), dtype=bool)
    vertices = np.empty((4, 4, 3), dtype=np.int64)
    weights = np.zeros((4, 4, 3), dtype=np.float64)
    for row in range(4):
        for column in range(4):
            node = row * 4 + column
            vertices[row, column] = (node, node, node)
            weights[row, column, 0] = 1.0
    for filename, array in {
        "lon.npy": lon,
        "lat.npy": lat,
        "cell_area_m2.npy": area,
        "bathymetry_m.npy": bathymetry,
        "mask_static.npy": static,
        "source_vertices.npy": vertices,
        "source_weights.npy": weights,
    }.items():
        np.save(surface_grid / filename, array, allow_pickle=False)
    grid_metadata = {
        "cache_schema_version": "3.0.0",
        "config_hash": "synthetic-paired",
        "domain": {"domain_id": domain},
    }
    _write_json(surface_grid / "metadata.json", grid_metadata)
    np.save(native_grid / "source_node_global_index.npy", np.arange(16, dtype=np.int64), allow_pickle=False)
    _write_json(native_grid / "metadata.json", grid_metadata)

    time_count = 48
    time_utc_ns = (
        np.datetime64("2025-01-01T00:00:00", "ns").astype(np.int64)
        + np.arange(time_count, dtype=np.int64) * 3_600_000_000_000
    )
    random = np.random.default_rng(20260806)
    surface_u = random.normal(scale=0.40, size=(time_count, 4, 4)).astype(np.float32)
    surface_v = random.normal(scale=0.35, size=(time_count, 4, 4)).astype(np.float32)
    eta = random.normal(scale=0.12, size=(time_count, 4, 4)).astype(np.float32)
    for filename, array in {
        "time_utc_ns.npy": time_utc_ns,
        "u_surface_mps.npy": surface_u,
        "v_surface_mps.npy": surface_v,
        "eta_m.npy": eta,
        "valid_mask_surface.npy": np.ones((time_count, 4, 4), dtype=bool),
    }.items():
        np.save(surface_month / filename, array, allow_pickle=False)

    z_levels = np.array([-60.0, -50.0, -40.0, -30.0, -20.0, -10.0, 0.0], dtype=np.float32)
    zcor = np.broadcast_to(z_levels, (time_count, 16, z_levels.size)).copy()
    hvel = random.normal(scale=0.45, size=(time_count, 16, z_levels.size, 2)).astype(np.float32)
    np.save(native_month / "time_utc_ns.npy", time_utc_ns, allow_pickle=False)
    np.save(native_month / "hvel.npy", hvel, allow_pickle=False)
    np.save(native_month / "zcor.npy", zcor, allow_pickle=False)
    month_metadata = {
        "status": "ready",
        "cache_kind": "standard_month",
        "cache_schema_version": "3.0.0",
        "config_hash": "synthetic-paired",
        "domain": {"domain_id": domain},
        "month": "202501",
    }
    _write_json(surface_month / "metadata.json", month_metadata)
    _write_json(native_month / "metadata.json", month_metadata)
    return surface_root, native_root, domain


def _make_config(
    root: Path,
    domain: str,
    *,
    dense_memory_limit_gib: float,
    label: str,
) -> Path:
    """建立符合正式 schema 的小型設定，並可強制測試 dense 或 PROPACK 路徑。"""

    upstream = root / "ocm_flow_domains.json"
    upstream_payload = {
        "schema_version": "3.0.0",
        "flow_domains": [
            {
                "flow_domain_id": domain,
                "name_zh": "合成完整 flow domain",
                "center": [120.015, 22.015],
                "bbox": [120.0, 120.03, 22.0, 22.03],
            }
        ],
    }
    _write_json(upstream, upstream_payload)
    config = {
        "schema_version": "1.0.0",
        "analysis_kind": "water_column_multivariate_svd",
        "analysis_label": label,
        "purpose": "合成六層聯合 SVD 測試。",
        "domain": {
            "flow_domain_id": domain,
            "name_zh": "合成完整 flow domain",
            "bbox_lon_lat": [120.0, 120.03, 22.0, 22.03],
            "bbox_order": "lon_min, lon_max, lat_min, lat_max",
            "center_lonlat": [120.015, 22.015],
            "source_flow_domains_config": str(upstream),
            "source_flow_domains_config_sha256": _sha256(upstream),
        },
        "input": {
            "years": [2025],
            "months": [1],
            "required_cache_schema_major": 3,
            "required_status": "ready",
            "required_cache_kinds": ["standard_month"],
            "expected_timestep_hours": 1.0,
            "maximum_source_gap_hours": 2.0,
            "time_axis_canonicalization_policy": "reject",
        },
        "vertical_sampling": {
            "surface_velocity_source": "published_ocm_surface_u_v",
            "fixed_depths_m_below_vertical_datum": [10.0, 20.0, 30.0, 40.0, 50.0],
            "vertical_interpolation": "linear_between_bracketing_finite_zcor_no_extrapolation",
            "eta_source": "published_ocm_surface_eta_m_once",
            "vertical_quadrature_weights_m": [5.0, 10.0, 10.0, 10.0, 10.0, 5.0],
        },
        "mask_and_missing_data": {
            "static_ocean_mask": "mask_static.npy",
            "minimum_feature_valid_fraction": 1.0,
            "minimum_retained_time_fraction": 1.0,
            "minimum_cells_per_velocity_level": 4,
            "minimum_eta_cells": 4,
            "missing_value_policy": "never_fill_nan_with_zero_or_interpolate",
        },
        "svd": {
            "variables": ["u_velocity_mps", "v_velocity_mps", "eta_m"],
            "normalization": "u_v_share_volume_weighted_rms__eta_uses_area_weighted_rms",
            "spatial_weight": "sqrt(cell_area_m2_times_vertical_quadrature_for_u_v__sqrt(cell_area_m2)_for_eta",
            "solver_policy": "direct_dense_lapack_then_direct_propack_streaming",
            "requested_mode_count": 20,
            "minimum_reported_mode_count": 20,
        },
        "solver": {
            "dense_solver": "numpy_linalg_svd_full_matrices_false",
            "streaming_solver": "scipy_svds_propack_linear_operator",
            "dense_memory_limit_gib": dense_memory_limit_gib,
            "operator_time_block_rows": 8,
            "propack_maxiter": 1000,
            "propack_max_attempts": 2,
            "relative_residual_tolerance": 1e-08,
            "random_seed": 20260806,
        },
        "parallel_execution": {
            "native_time_block_size": 12,
            # 合成 cache 也以兩個 worker 驗證完成順序不影響 canonical UTC row 與輸出維度。
            "native_io_workers": 2,
            "linear_algebra_threads": 1,
        },
        "figures": {
            "mode_count": 20,
            "max_quiver_arrows_per_axis": 6,
            "output_formats": ["png"],
            "raster_dpi": 72,
        },
    }
    path = root / f"{label}.json"
    _write_json(path, config)
    return path


class WaterColumnMultivariateSvdTest(unittest.TestCase):
    """驗證單次六層聯合 SVD 的輸出維度、eta 語意與兩種直接求解路徑。"""

    def test_plot_domain_name_removes_internal_flow_cache_suffixes(self) -> None:
        """圖面標題只保留研究海域名稱，不把資料治理後綴當成地名。"""

        self.assertEqual(_plot_domain_name_zh("後灣海生館完整 flow domain"), "後灣海生館海域")
        self.assertEqual(_plot_domain_name_zh("貢寮單區域 flow cache"), "貢寮海域")

    def test_dense_direct_svd_outputs_six_velocity_levels_and_one_eta(self) -> None:
        """dense 路徑應發布 20 模態、六層速度與沒有 depth 軸的 eta。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=1.0,
                label="synthetic_water_dense_v1",
            )
            loaded = load_water_column_config(config_path)
            self.assertEqual(loaded.fixed_depths_m, (10.0, 20.0, 30.0, 40.0, 50.0))
            # 測試設定未指定策略時，loader 必須採可稽核的 selected-node 預設值；不能依
            # 合成格網大小、機器記憶體或執行順序改變 native 輸入集合。
            self.assertEqual(loaded.native_block_read_strategy, "selected_nodes_fancy_index")
            output = run_water_column_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "results",
                # 正式需求要求每個 run 都產出 20 個模態的獨立圖面；合成格網很小，故在此
                # 端到端測試直接啟用繪圖，確保拆分後的檔名、圖數與 renderer 不會只在
                # SERVER 的正式資料上才暴露錯誤。
                make_figures=True,
            )
            mode_u = np.load(output / "mode_u_mps_per_raw_pc.npy", allow_pickle=False)
            mode_eta = np.load(output / "mode_eta_m_per_raw_pc.npy", allow_pickle=False)
            pc = np.load(output / "pc.npy", allow_pickle=False)
            self.assertEqual(mode_u.shape, (20, 6, 4, 4))
            self.assertEqual(mode_eta.shape, (20, 4, 4))
            self.assertEqual(pc.shape, (20, 48))
            metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["svd"]["solver"]["strategy"], "direct_dense_lapack")
            self.assertEqual(metadata["figures"]["mode_figure_count"], 20)
            self.assertEqual(metadata["figures"]["independent_figure_count"], 168)
            self.assertTrue(metadata["figures"]["independent_figure_policy"])
            report_dir = output / "figures" / "report"
            plot_metadata = json.loads((output / "figures" / "plot_metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(plot_metadata["independent_figure_policy"]["enabled"])
            self.assertEqual(plot_metadata["independent_figure_policy"]["logical_figure_count"], 168)
            # 每個速度主圖本身必須已含比例尺，只保留一份透明比例尺供必要的後製移位；
            # 因此每個 mode 是六張速度主圖、六張透明素材、一張 eta 與一張 PC，而不是
            # 另複製一張 ``_with_vector_scale``。這可避免使用者誤選沒有比例尺的主圖。
            expected_mode_png_count = 20 * (len(VELOCITY_LEVEL_IDS) * 2 + 2)
            self.assertEqual(len(list(report_dir.glob("water_column_mode_*.png"))), expected_mode_png_count)
            self.assertIn("主圖右下角", plot_metadata["rendering"]["vector_scale_policy"])
            for mode_number in (1, 20):
                for level_id in VELOCITY_LEVEL_IDS:
                    stem = f"water_column_mode_{mode_number:02d}_{level_id}_spatial_report"
                    self.assertTrue((report_dir / f"{stem}.png").is_file())
                    self.assertTrue((report_dir / f"{stem}_vector_scale_transparent.png").is_file())
                    self.assertFalse((report_dir / f"{stem}_with_vector_scale.png").exists())
                self.assertTrue(
                    (report_dir / f"water_column_mode_{mode_number:02d}_eta_spatial_report.png").is_file()
                )
                self.assertTrue((report_dir / f"water_column_mode_{mode_number:02d}_pc_report.png").is_file())
            # 舊版合併畫布與舊版根目錄圖檔必須不存在；後製者只需要從 report/ 取單一資產。
            self.assertEqual(list((output / "figures").glob("mode_*.png")), [])
            self.assertFalse((output / "figures" / "feature_coverage_qc.png").exists())
            self.assertEqual(metadata["mask_and_missing_data"]["velocity_feature_cell_counts"]["z_minus_050m"], 16)
            self.assertEqual(metadata["resources"]["parallel_execution"]["native_io_workers_effective"], 2)
            self.assertEqual(
                metadata["resources"]["native_block_read_strategy"]["strategy"],
                # 合成 grid 的 16 個 node 完全連續；即使來源節點樣態不同，設定明示的
                # selected-node 策略仍須完整傳入 metadata，避免 I/O 決策隱藏於執行環境。
                "selected_nodes_fancy_index",
            )

    def test_read_only_replot_uses_existing_water_column_arrays_without_solving(self) -> None:
        """水柱重繪必須只讀既有 run，並將新版圖面發布到另一個 immutable bundle。

        此測試先以 ``--no-figures`` 等價的 runner 取得小型完整科學成果，再以不同 figures
        設定呼叫 replot。測試對來源 metadata、config 與所有 renderer 輸入陣列逐檔雜湊，
        並把 SVD solver mock 成必定失敗，證明重繪過程既不改寫來源、也不會重新求解或碰觸
        上游 paired cache；輸出只應存在於獨立的 figure bundle 根目錄。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=1.0,
                label="synthetic_water_read_only_replot_v1",
            )
            source_run = run_water_column_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "science_results",
                make_figures=False,
            )
            self.assertFalse((source_run / "figures").exists())
            source_filenames = ("metadata.json", "config.json", *WATER_COLUMN_REPLOT_REQUIRED_ARRAY_FILENAMES)
            source_hashes_before = {
                filename: _sha256(source_run / filename) for filename in source_filenames
            }

            # 以完整設定複製明確改動一個純圖面欄位；年份、深度、遮罩與 SVD 參數保持原樣，
            # 使 replot 的 science-config hash 檢查能驗證「只改視覺、不改科學」契約。
            render_config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            render_config_payload["figures"]["max_quiver_arrows_per_axis"] = 5
            render_config_path = root / "synthetic_water_read_only_replot_render_config.json"
            _write_json(render_config_path, render_config_payload)
            with patch(
                "ocm_svd_analysis.water_column_multivariate_svd._solve_direct_svd",
                side_effect=AssertionError("純重繪不得再次呼叫直接 SVD solver"),
            ):
                bundle_dir = replot_water_column_multivariate_svd(
                    run_dir=source_run,
                    output_root=root / "figure_bundles",
                    config_path=render_config_path,
                )

            # macOS 的暫存目錄可能以 ``/var`` 建立、以 ``/private/var`` 回傳 resolved
            # path；兩者是同一檔案系統位置。重繪 API 明確回傳 canonical path，因此比對前
            # 必須同樣正規化預期值，避免把平台路徑別名誤判成發布位置錯誤。
            self.assertEqual(
                bundle_dir,
                (
                    root
                    / "figure_bundles"
                    / "water_column_svd_figure_bundles"
                    / source_run.name
                    # v2 的主圖已內嵌比例尺、地圖標題與色條分欄，故必須發布成新的
                    # immutable style，不能覆寫使用者已檢視過的 v1 圖包。
                    / "academic_report_ready_water_column_independent_v2"
                ).resolve(),
            )
            source_hashes_after = {
                filename: _sha256(source_run / filename) for filename in source_filenames
            }
            self.assertEqual(source_hashes_after, source_hashes_before)
            self.assertFalse((source_run / "figures").exists())
            bundle_metadata = json.loads((bundle_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertFalse(bundle_metadata["source_run"]["surface_cache_read"])
            self.assertFalse(bundle_metadata["source_run"]["native_cache_read"])
            self.assertFalse(bundle_metadata["source_run"]["svd_solver_called"])
            self.assertEqual(bundle_metadata["source_run"]["source_files_sha256"], source_hashes_before)
            self.assertEqual(
                bundle_metadata["figures"]["renderer_info"]["logical_figure_count"],
                168,
            )
            report_dir = bundle_dir / "figures" / "report"
            self.assertTrue((report_dir / "water_column_mode_01_surface_spatial_report.png").is_file())
            self.assertTrue((report_dir / "water_column_mode_20_pc_report.png").is_file())
            # 唯讀 replot 同樣應以主圖內嵌比例尺，避免重繪後重新引入舊版無比例尺主圖。
            self.assertEqual(len(list(report_dir.glob("water_column_mode_*.png"))), 280)
            self.assertEqual(list((bundle_dir / "figures").glob("mode_*.png")), [])

    def test_host_feature_by_time_layout_is_exact_transpose_permutation_and_roundtrips_maps(self) -> None:
        """主持人版必須依 eta→all-u→all-v 發布，並由 U_A 精確回填六層圖場。

        此測試在小型完整 run 上先取得直接 dense SVD，再把來源檔逐一雜湊。host-layout
        exporter 不可呼叫 solver、不可碰 native/surface cache、不可修改來源；它輸出的
        ``A=(feature,time)`` 因子應維持同一組奇異值，且從 compact 左奇異向量回填的圖場必須
        與來源 physical mode 在有效遮罩內逐值相同、無效格點保持 NaN。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=1.0,
                label="synthetic_water_host_layout_v1",
            )
            source_run = run_water_column_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "science_results",
                make_figures=False,
            )
            source_hashes_before = {
                filename: _sha256(source_run / filename)
                for filename in HOST_LAYOUT_REQUIRED_SOURCE_FILENAMES
            }
            with patch(
                "ocm_svd_analysis.water_column_multivariate_svd._solve_direct_svd",
                side_effect=AssertionError("主持人版轉置／重排不得再次呼叫 SVD solver"),
            ):
                host_output = export_water_column_host_layout(
                    run_dir=source_run,
                    output_root=root / "host_layout_results",
                )

            expected_feature_count = 16 + 2 * len(VELOCITY_LEVEL_IDS) * 16
            self.assertEqual(
                host_output,
                (
                    root
                    / "host_layout_results"
                    / "water_column_svd_host_layout"
                    / source_run.name
                    / HOST_LAYOUT_ID
                ).resolve(),
            )
            host_metadata = json.loads((host_output / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(host_metadata["matrix"]["shape"], [expected_feature_count, 48])
            self.assertEqual(host_metadata["matrix"]["orientation"], "feature_by_time")
            self.assertEqual(
                host_metadata["matrix"]["state_vector_order"],
                [
                    "eta_surface_once",
                    "u_surface_then_z010_z020_z030_z040_z050",
                    "v_surface_then_z010_z020_z030_z040_z050",
                ],
            )
            self.assertFalse(host_metadata["source_run"]["svd_solver_called"])
            self.assertFalse(host_metadata["source_run"]["native_cache_read"])
            self.assertFalse(host_metadata["source_run"]["surface_cache_read"])
            self.assertEqual(
                np.load(host_output / "left_singular_vectors_weighted.npy", allow_pickle=False).shape,
                (expected_feature_count, 20),
            )
            self.assertEqual(
                np.load(host_output / "right_singular_vectors_time.npy", allow_pickle=False).shape,
                (20, 48),
            )
            self.assertTrue(
                np.array_equal(
                    np.load(host_output / "singular_values.npy", allow_pickle=False),
                    np.load(source_run / "singular_values.npy", allow_pickle=False),
                )
            )

            # 對照圖中 q(t) 的區塊：前 16 列 eta，接著每層 16 列 u，最後才是同順序 v。
            map_rows = (host_output / "feature_index_map.csv").read_text(encoding="utf-8").splitlines()
            self.assertIn(
                ",eta,eta_surface,0.0,",
                map_rows[1],
            )
            self.assertIn(
                ",u,surface,0.0,",
                map_rows[1 + 16],
            )
            self.assertIn(
                ",v,surface,0.0,",
                map_rows[1 + 16 + len(VELOCITY_LEVEL_IDS) * 16],
            )

            for filename in (
                "roundtrip_mode_u_mps_per_raw_pc.npy",
                "roundtrip_mode_v_mps_per_raw_pc.npy",
                "roundtrip_mode_eta_m_per_raw_pc.npy",
            ):
                self.assertTrue((host_output / filename).is_file())
            self.assertTrue(
                np.array_equal(
                    np.load(host_output / "roundtrip_mode_u_mps_per_raw_pc.npy", allow_pickle=False),
                    np.load(source_run / "mode_u_mps_per_raw_pc.npy", allow_pickle=False),
                    equal_nan=True,
                )
            )
            self.assertTrue(
                np.array_equal(
                    np.load(host_output / "roundtrip_mode_eta_m_per_raw_pc.npy", allow_pickle=False),
                    np.load(source_run / "mode_eta_m_per_raw_pc.npy", allow_pickle=False),
                    equal_nan=True,
                )
            )
            self.assertLessEqual(
                max(
                    host_metadata["roundtrip_validation"]["max_abs_difference_mode_u_mps_per_raw_pc"],
                    host_metadata["roundtrip_validation"]["max_abs_difference_mode_v_mps_per_raw_pc"],
                    host_metadata["roundtrip_validation"]["max_abs_difference_mode_eta_m_per_raw_pc"],
                    host_metadata["roundtrip_validation"]["max_abs_difference_pc"],
                ),
                1e-12,
            )
            source_hashes_after = {
                filename: _sha256(source_run / filename)
                for filename in HOST_LAYOUT_REQUIRED_SOURCE_FILENAMES
            }
            self.assertEqual(source_hashes_after, source_hashes_before)

    def test_propack_streaming_path_returns_valid_twenty_modes(self) -> None:
        """強制低 RAM 預算時，PROPACK 應直接取得並驗證前 20 個奇異三元組。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=0.000001,
                label="synthetic_water_propack_v1",
            )
            output = run_water_column_multivariate_svd(
                config_path=config_path,
                native_root=native_root,
                surface_root=surface_root,
                output_root=root / "results",
                make_figures=False,
            )
            metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            solver = metadata["svd"]["solver"]
            self.assertEqual(solver["strategy"], "direct_propack_streaming")
            self.assertLessEqual(max(metadata["svd"]["left_relative_residuals"]), 1e-8)
            self.assertLessEqual(max(metadata["svd"]["right_relative_residuals"]), 1e-8)
            self.assertEqual(len(np.load(output / "singular_values.npy", allow_pickle=False)), 20)

    def test_propack_accepts_documented_streaming_residual_floor(self) -> None:
        """streaming 重算殘差略高於設定值時，應以可稽核數值地板發布。

        正式兩年矩陣使用 memory-map 分塊乘法；PROPACK 收斂後再以獨立分塊計算殘差，可能因
        浮點累加得到約數個 ``1e-9`` 的值。本測試刻意注入 ``2.5e-9`` 殘差與充分小的正交性
        誤差，確認程式不會重建原始資料，而會以明載 ``1e-8`` 數值地板接受，並將設定值、
        有效門檻與 accepted 狀態寫進 solver metadata。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=0.000001,
                label="synthetic_water_propack_residual_floor_v1",
            )
            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            # 使用比數值地板更嚴格的設定，驗證有效門檻不是悄悄覆寫輸入，而是由 metadata
            # 清楚揭露的 streaming 驗證政策。
            config_payload["solver"]["relative_residual_tolerance"] = 1e-9
            _write_json(config_path, config_payload)
            config = load_water_column_config(config_path)

            matrix_shape = (48, 25)
            matrix_path = root / "weighted_anomaly_float64.dat"
            matrix = np.memmap(matrix_path, dtype=np.float64, mode="w+", shape=matrix_shape)
            matrix[:] = np.random.default_rng(20260806).normal(size=matrix_shape)
            matrix.flush()
            total_sum_squares = float(np.sum(np.asarray(matrix) ** 2))
            del matrix

            random = np.random.default_rng(20260807)
            left_vectors, _ = np.linalg.qr(random.normal(size=(matrix_shape[0], 20)))
            right_vectors, _ = np.linalg.qr(random.normal(size=(matrix_shape[1], 20)))
            singular_values = np.linspace(20.0, 1.0, 20, dtype=np.float64)
            with patch(
                "scipy.sparse.linalg.svds",
                return_value=(left_vectors, singular_values, right_vectors.T),
            ), patch(
                "ocm_svd_analysis.water_column_multivariate_svd._compute_residuals",
                return_value=(
                    np.full(20, 2.5e-9, dtype=np.float64),
                    np.full(20, 2.5e-9, dtype=np.float64),
                    5.0e-13,
                ),
            ):
                result = _solve_direct_svd(
                    matrix_path,
                    matrix_shape=matrix_shape,
                    config=config,
                    total_sum_squares=total_sum_squares,
                )

            solver = result.solver_metadata
            self.assertEqual(solver["configured_relative_residual_tolerance"], 1e-9)
            self.assertEqual(solver["streaming_relative_residual_numerical_floor"], 1e-8)
            self.assertEqual(solver["acceptance_relative_residual_tolerance"], 1e-8)
            self.assertEqual(
                solver["attempts"][0]["status"],
                "accepted_streaming_numerical_residual_floor",
            )

    def test_solver_failure_preserves_checkpoint_and_resume_reuses_weighted_matrix(self) -> None:
        """求解器失敗後應保留 checkpoint，續跑不可重讀 paired native 3D 資料。

        以 mock 在 SVD 呼叫點注入可預期例外，模擬 SERVER 上 PROPACK／殘差驗證失敗；首次 run
        必須把已完成的 float64 加權矩陣與欄位布局移到 recovery 目錄。第二次從同一目錄續跑
        時，不應再次呼叫 raw feature 寫入流程，而要直接載入 checkpoint 後完成正式原子發布。
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            surface_root, native_root, domain = _make_paired_cache(root)
            # 刻意讓一個表層 u/v cell 未達 100% 有效率，使 selected feature 少於完整
            # candidate raw 軸。這正是正式兩年資料因濕乾或 zcor 無法包夾而會出現的情境，
            # 可防止 checkpoint loader 再次把候選 QC 向量誤認成 selected 向量。
            surface_u_path = surface_root / domain / "months" / "202501" / "u_surface_mps.npy"
            surface_u = np.load(surface_u_path, allow_pickle=False)
            surface_u[0, 0, 0] = np.nan
            np.save(surface_u_path, surface_u, allow_pickle=False)
            label = "synthetic_water_recovery_v1"
            config_path = _make_config(
                root,
                domain,
                dense_memory_limit_gib=1.0,
                label=label,
            )
            output_root = root / "results"
            with patch(
                "ocm_svd_analysis.water_column_multivariate_svd._solve_direct_svd",
                side_effect=RuntimeError("synthetic solver failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic solver failure"):
                    run_water_column_multivariate_svd(
                        config_path=config_path,
                        native_root=native_root,
                        surface_root=surface_root,
                        output_root=output_root,
                        make_figures=False,
                    )

            namespace = output_root / "water_column_svd"
            recovery_directories = list(namespace.glob(f".{label}.recovery-*"))
            self.assertEqual(len(recovery_directories), 1)
            recovery = recovery_directories[0]
            self.assertTrue((recovery / "weighted_anomaly_float64.dat").is_file())
            self.assertTrue((recovery / "solver_resume_checkpoint.npz").is_file())
            with np.load(recovery / "solver_resume_checkpoint.npz", allow_pickle=False) as checkpoint:
                self.assertGreater(
                    checkpoint["selected_feature_valid_fraction"].size,
                    checkpoint["selected_raw_columns"].size,
                )
            diagnostic = json.loads((recovery / "solver_failure_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["status"], "failed_recoverable")
            self.assertIn("synthetic solver failure", diagnostic["exception_message"])

            # 若 resume 路徑意外回頭 materialize raw feature，本 mock 會立即讓測試失敗；因此
            # 成功代表原生固定深度內插結果確實由同一個 checkpoint 加權矩陣重用。
            with patch(
                "ocm_svd_analysis.water_column_multivariate_svd._write_raw_feature_matrix",
                side_effect=AssertionError("resume 不得重新建立 raw feature matrix"),
            ):
                output = run_water_column_multivariate_svd(
                    config_path=config_path,
                    native_root=native_root,
                    surface_root=surface_root,
                    output_root=output_root,
                    make_figures=False,
                    resume_partial_dir=recovery,
                )
            self.assertTrue((output / "metadata.json").is_file())
            self.assertFalse((output / "weighted_anomaly_float64.dat").exists())
            self.assertFalse((output / "solver_resume_checkpoint.npz").exists())
            self.assertFalse(recovery.exists())


if __name__ == "__main__":
    unittest.main()
