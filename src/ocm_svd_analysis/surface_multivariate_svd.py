"""由 OCM surface cache 建立表層 u/v/eta 三變數 SVD。

本模組的輸入只能是 OCM-Data-Preprocessing 已發布的 schema 3 `ocm_surface` 快取。每個
月份的 u、v、eta 與有效遮罩都以 NumPy memory-map 開啟，再只複製候選 focus bbox 的
連續 `(time, lat, lon)` 小窗進入分析矩陣；程式絕不開啟 SCHISM 原始 NetCDF，也不把缺值
或陸地改成 0。

「SVD」是研究團隊使用的完整名稱。數值求解採較省記憶體的空間協方差特徵分解：當
加權距平矩陣 `X_w` 的形狀為 `(3P, N)`（`P` 個共同有效海域格點、`N` 個完整時次）時，
程式實際求解 `C = X_w X_w^T/(N-1) = U Lambda U^T`，而非直接呼叫 `np.linalg.svd`。
由 `lambda_k = sigma_k^2/(N-1)` 可回復奇異值 `sigma_k`；因此此作法與薄型 SVD
`X_w = U Sigma V^T` 完全等價，時間係數則以 `PC = U^T X_w = Sigma V^T` 直接回復。
選此解法的前提是本專案的空間自由度 `3P` 遠少於時間樣本數 `N`；若未來改為大 AOI、
高解析度或短時間窗而使此前提不成立，應重新評估改用時間協方差或截斷 SVD 求解器。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from threadpoolctl import threadpool_limits

from .performance import PerformanceRecorder


CONFIG_SCHEMA_VERSION = "1.0.0"
"""本 SVD 設定檔的版本；與上游 surface cache schema 分開管理。"""

SURFACE_CACHE_SCHEMA_MAJOR = 3
"""本版讀取器接受的 OCM surface cache schema 主版號。"""

COMPONENT_NAMES = ("u", "v", "eta")
"""三變數 SVD 狀態向量的固定分量順序，亦是輸出 imputation mask 的 component 軸順序。"""


@dataclass(frozen=True)
class TimeAxisRepair:
    """一段已知來源時間軸錯標的顯式修正規則。

    `start_index:stop_index` 是單月快取 time 軸的半開區間；只有原始起訖 UTC 與設定完全
    相符時才會套用 `shift_nanoseconds`。這種雙重鎖定避免日後上游快取已修正或換版後，
    舊規則仍悄悄移動正確時間。修正只改分析程序記憶體中的時間座標，不覆寫上游 `.npy`
    或 u/v/eta 數值；原因、樣本數與位移會寫入成果 metadata。
    """

    month_id: str
    start_index: int
    stop_index: int
    shift_hours: float
    shift_nanoseconds: int
    expected_original_start_ns: int
    expected_original_end_ns: int
    reason: str


@dataclass(frozen=True)
class AnalysisConfig:
    """經驗證的單一表層 SVD run 設定。

    bbox 固定採 `(lon_min, lon_max, lat_min, lat_max)` 順序；月份快取的位置、輸出位置與
    SERVER 絕對路徑不寫在設定內，而由 CLI 注入。這可讓同一設定在本機 smoke test 與
    SERVER 正式執行間保持完全相同的科學參數。
    """

    raw: dict[str, Any]
    analysis_label: str
    focus_id: str
    focus_name_zh: str
    approval_status: str
    domain_id: str
    bbox: tuple[float, float, float, float]
    anchor_lonlat: tuple[float, float]
    analysis_unit_id: str | None
    source_analysis_units_config: str | None
    source_analysis_units_config_sha256: str | None
    spatial_mask_policy: str
    years: tuple[int, ...]
    months: tuple[int, ...]
    required_status: str
    required_cache_kinds: frozenset[str]
    expected_timestep_hours: float
    maximum_source_gap_hours: float
    known_time_axis_repairs: tuple[TimeAxisRepair, ...]
    minimum_static_ocean_cells: int
    minimum_cell_valid_fraction: float
    max_interpolation_steps: int
    minimum_retained_time_fraction: float
    requested_mode_count: int
    minimum_reported_mode_count: int
    io_workers: int
    linear_algebra_threads: int
    figure_mode_count: int
    max_quiver_arrows_per_axis: int
    figure_style: str
    figure_formats: tuple[str, ...]
    figure_dpi: int
    figure_transparent_background: bool


@dataclass(frozen=True)
class SourceMonth:
    """一個實際讀取的 surface 月份及其可追溯 metadata 摘要。

    `metadata_sha256` 是月份 metadata 的內容雜湊，能偵測來源設定、資料狀態或來源清單的
    改動；為避免把私有 SERVER 絕對路徑寫入成果，輸出只保存 domain/month 與這個雜湊。
    """

    month_id: str
    cache_kind: str
    metadata_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MonthFocusChunk:
    """單一月份平行 I/O 完成後交回主執行緒的 focus 小窗。

    每個 worker 都獨立開啟該月 `.npy` memory-map、驗證 metadata 與 shape，並只複製設定
    bbox 的 u/v/eta/valid 小窗。worker 間不共享可變陣列；主執行緒依月份排序後才串接，
    因此平行讀取不會改變 SVD 的時間順序或數值結果。
    """

    year: int
    month: int
    source: SourceMonth
    time_utc_ns: np.ndarray
    fields: np.ndarray
    valid_surface: np.ndarray
    repaired_time_step_count: int


@dataclass(frozen=True)
class LoadedSurfaceData:
    """從各月小窗串接而成的表層資料與靜態格網。

    `fields` 維度為 `(time, component, lat, lon)`，分量順序是 u、v、eta；`valid_surface`
    是上游提供的 `(time, lat, lon)` 表層流場有效遮罩。`analysis_geometry_mask` 是以分析
    邊界的 cell-center inclusion 產生的靜態遮罩：讀取小窗為了 I/O 安全可外擴一格，但
    小窗外擴出的格點絕不能因而進入 SVD。所有數值仍保留 NaN，缺值處理僅在
    `prepare_analysis_data` 依明確規則執行。
    """

    lon: np.ndarray
    lat: np.ndarray
    cell_area_m2: np.ndarray
    mask_static: np.ndarray
    analysis_geometry_mask: np.ndarray
    fields: np.ndarray
    valid_surface: np.ndarray
    time_utc_ns: np.ndarray
    lat_slice: slice
    lon_slice: slice
    source_months: tuple[SourceMonth, ...]
    repaired_time_step_count: int


@dataclass(frozen=True)
class PreparedAnalysisData:
    """可進入 SVD 的固定空間遮罩、時間樣本與短缺值處理結果。

    `series` 的維度為 `(retained_time, component, ocean_cell)`；這個緊湊表示法避免把陸地
    與未選定的 bbox cell 放進矩陣。`imputed` 僅標記依法插補的短缺值，方便日後將其排除
    做不補值敏感度分析。
    """

    time_utc_ns: np.ndarray
    series: np.ndarray
    common_mask: np.ndarray
    cell_triplet_valid_fraction: np.ndarray
    imputed: np.ndarray
    retained_time_fraction: float
    initial_time_count: int
    common_ocean_cell_count: int


@dataclass(frozen=True)
class SvdSolution:
    """三變數面積加權標準化 SVD 的數值結果。

    `spatial_vectors` 是已反轉成物理分量尺度前、且已固定正負號的空間 SVD 向量；`pc` 為
    `Sigma V^T`，因此和輸出的物理 loading 相乘可重建距平。u/v 共用速度尺度，以避免
    改變水平向量的旋轉對稱性；eta 使用獨立高度尺度，避免 m 與 m/s 直接混合。
    """

    spatial_vectors: np.ndarray
    pc: np.ndarray
    singular_values: np.ndarray
    explained_variance: np.ndarray
    cumulative_explained_variance: np.ndarray
    all_explained_variance: np.ndarray
    mean_by_component: np.ndarray
    velocity_rms_mps: float
    eta_rms_m: float
    anchor_cell_index: int
    sign_sources: tuple[str, ...]
    full_rank_relative_reconstruction_error: float
    retained_mode_relative_reconstruction_error: float
    orthogonality_max_abs_error: float
    linear_algebra_threads: int


@dataclass(frozen=True)
class AcademicVisualizationFields:
    """供論文圖與後製使用的標準化 PC 及物理量回歸空間模態。

    `pc_standardized` 的每個模態皆以樣本標準差（ddof=1）正規化為無因次時間係數；
    `regression_*` 則是原物理 loading 乘回同一 PC 標準差，單位分別為 m/s 與 m per
    1 standard deviation of PC。兩者相乘仍代表該模態的物理距平重建，但圖面不再出現
    極小 loading 與巨大加權 PC，且可直接解讀「PC 增加 1σ 時的局地變化」。
    """

    pc_standardized: np.ndarray
    pc_standard_deviation: np.ndarray
    regression_u: np.ndarray
    regression_v: np.ndarray
    regression_eta: np.ndarray
    pc_mean_max_abs: float


def _require(condition: bool, message: str) -> None:
    """在資料契約不成立時立即停止，避免產生外觀正常但科學意義錯誤的 SVD。"""

    if not condition:
        raise ValueError(message)


def _read_json_object(path: Path) -> dict[str, Any]:
    """讀取 JSON object，拒絕清單、空檔與非物件根節點。"""

    if not path.is_file():
        raise FileNotFoundError(f"找不到 JSON 檔: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 根節點必須是物件: {path}")
    return payload


def _canonical_json_hash(payload: object) -> str:
    """回傳排序後 JSON 的 SHA-256，供設定與來源 metadata 的可重現識別使用。"""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_float(value: object, field: str) -> float:
    """驗證設定數值為有限浮點數，避免 NaN/Infinity 進入座標、時間或權重計算。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必須是數值")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{field} 必須是有限數值")
    return result


def _as_positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    """驗證正整數設定，保護模式數、格點數與插補長度等離散規則。"""

    if isinstance(value, bool) or not isinstance(value, int) or (value < 0 if allow_zero else value < 1):
        noun = "非負整數" if allow_zero else "正整數"
        raise ValueError(f"{field} 必須是{noun}")
    return value


def _as_closed_fraction(value: object, field: str) -> float:
    """驗證比例門檻在 0 到 1 之間，防止建立不可能的 coverage 規則。"""

    result = _as_float(value, field)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} 必須介於 0 與 1")
    return result


def _parse_utc_ns(value: object, field: str) -> int:
    """把設定中的 ISO 8601 UTC 時刻轉成 epoch ns，拒絕含糊格式或非奈秒可表示時刻。

    設定必須以尾碼 `Z` 明確表示 UTC。NumPy 的 `datetime64` 不保存時區，因此只在確認
    `Z` 後移除尾碼並轉換；輸出仍使用原始設定與 UTC epoch ns，不依賴 SERVER 本機時區。
    """

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} 必須是以 Z 結尾的 ISO 8601 UTC 時刻")
    try:
        return int(np.datetime64(value[:-1], "ns").astype(np.int64))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{field} 不是可解析的 UTC 時刻") from error


def _load_known_time_axis_repairs(
    input_config: dict[str, Any],
    years: tuple[int, ...],
    months: tuple[int, ...],
    expected_timestep_hours: float,
) -> tuple[TimeAxisRepair, ...]:
    """驗證可重現的已知時間軸修正，並轉成只供單月讀取器使用的不可變規則。

    預設沒有任何修正。每條規則必須鎖定月份、索引區間、原始起訖時間與非零小時位移；
    區間長度還必須符合設定的固定時間步。這個機制只處理已有來源證據的時間座標錯標，
    不能拿來填補缺日、建立新樣本或改變任一流場數值。
    """

    repairs_raw = input_config.get("known_time_axis_repairs", [])
    if not isinstance(repairs_raw, list):
        raise ValueError("input.known_time_axis_repairs 必須是清單")
    configured_month_ids = {f"{year}{month:02d}" for year in years for month in months}
    repairs: list[TimeAxisRepair] = []
    used_ranges: set[tuple[str, int, int]] = set()
    expected_step_ns = int(round(expected_timestep_hours * 3_600_000_000_000.0))
    for index, item in enumerate(repairs_raw):
        field = f"input.known_time_axis_repairs[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field} 必須是物件")
        month_id = item.get("month")
        reason = item.get("reason")
        if not isinstance(month_id, str) or month_id not in configured_month_ids:
            raise ValueError(f"{field}.month 必須是本次設定包含的 YYYYMM")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{field}.reason 必須是非空白文字")
        start_index = _as_positive_int(item.get("start_index"), f"{field}.start_index", allow_zero=True)
        stop_index = _as_positive_int(item.get("stop_index"), f"{field}.stop_index")
        _require(stop_index > start_index, f"{field} 必須滿足 stop_index > start_index")
        range_key = (month_id, start_index, stop_index)
        _require(range_key not in used_ranges, f"{field} 與前一規則重複")
        used_ranges.add(range_key)
        shift_hours = _as_float(item.get("shift_hours"), f"{field}.shift_hours")
        _require(shift_hours != 0.0, f"{field}.shift_hours 不可為 0")
        shift_nanoseconds_float = shift_hours * 3_600_000_000_000.0
        shift_nanoseconds = int(round(shift_nanoseconds_float))
        _require(
            abs(shift_nanoseconds_float - shift_nanoseconds) < 0.5,
            f"{field}.shift_hours 無法精確表示為整數奈秒",
        )
        expected_start_ns = _parse_utc_ns(item.get("expected_original_start_utc"), f"{field}.expected_original_start_utc")
        expected_end_ns = _parse_utc_ns(item.get("expected_original_end_utc"), f"{field}.expected_original_end_utc")
        _require(expected_end_ns >= expected_start_ns, f"{field} 原始結束時間不可早於開始時間")
        expected_count = (expected_end_ns - expected_start_ns) // expected_step_ns + 1
        _require(
            expected_count == stop_index - start_index,
            f"{field} 索引長度與原始起訖時間、設定步長不一致",
        )
        repairs.append(
            TimeAxisRepair(
                month_id=month_id,
                start_index=start_index,
                stop_index=stop_index,
                shift_hours=shift_hours,
                shift_nanoseconds=shift_nanoseconds,
                expected_original_start_ns=expected_start_ns,
                expected_original_end_ns=expected_end_ns,
                reason=reason,
            )
        )
    return tuple(repairs)


def load_analysis_config(config_path: Path) -> AnalysisConfig:
    """讀取並驗證表層三變數 SVD 設定。

    本函式刻意不接受未列於設定的分析變數、bbox 順序或來源年月。這讓明日的貢寮 pilot
    可被完整重跑；`input.year` 是既有單年設定的相容寫法，新的兩年分析必須以
    `input.years: [2024, 2025]` 明確建立另一份有版本的設定檔。
    """

    raw = _read_json_object(config_path)
    _require(raw.get("schema_version") == CONFIG_SCHEMA_VERSION, f"設定檔必須使用 schema {CONFIG_SCHEMA_VERSION}")
    _require(raw.get("analysis_kind") == "surface_multivariate_svd", "analysis_kind 必須是 surface_multivariate_svd")

    label = raw.get("analysis_label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("analysis_label 必須是非空白文字")

    focus = raw.get("focus")
    if not isinstance(focus, dict):
        raise ValueError("focus 必須是物件")
    focus_id = focus.get("focus_id")
    focus_name = focus.get("name_zh")
    approval_status = focus.get("approval_status")
    domain_id = focus.get("flow_domain_id")
    for field, value in (("focus_id", focus_id), ("name_zh", focus_name), ("approval_status", approval_status), ("flow_domain_id", domain_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"focus.{field} 必須是非空白文字")
    _require(approval_status in {"candidate", "approved"}, "focus.approval_status 必須是 candidate 或 approved")
    bbox_raw = focus.get("bbox_lon_lat")
    if not isinstance(bbox_raw, list) or len(bbox_raw) != 4:
        raise ValueError("focus.bbox_lon_lat 必須是 [lon_min, lon_max, lat_min, lat_max]")
    lon_min, lon_max, lat_min, lat_max = (_as_float(value, "focus.bbox_lon_lat") for value in bbox_raw)
    _require(lon_min < lon_max and lat_min < lat_max, "focus.bbox_lon_lat 必須滿足 min < max")
    anchor_raw = focus.get("anchor_lonlat")
    if not isinstance(anchor_raw, list) or len(anchor_raw) != 2:
        raise ValueError("focus.anchor_lonlat 必須是 [lon, lat]")
    anchor_lon, anchor_lat = (_as_float(value, "focus.anchor_lonlat") for value in anchor_raw)
    _require(lon_min <= anchor_lon <= lon_max and lat_min <= anchor_lat <= lat_max, "focus anchor 必須位於 bbox 內")

    # 既有單區 pilot 未宣告此欄位時維持 reader window 行為；六區正式批次設定則必須明確
    # 選用 cell-center 遮罩，避免為 memory-map 安全外擴的一格被誤放進統計狀態向量。
    spatial_mask_policy = focus.get("spatial_mask_policy", "reader_window")
    _require(
        spatial_mask_policy in {"reader_window", "analysis_bbox_cell_center"},
        "focus.spatial_mask_policy 必須是 reader_window 或 analysis_bbox_cell_center",
    )
    analysis_unit_id = focus.get("analysis_unit_id")
    source_analysis_units_config = focus.get("source_analysis_units_config")
    source_analysis_units_config_sha256 = focus.get("source_analysis_units_config_sha256")
    if spatial_mask_policy == "analysis_bbox_cell_center":
        for field, value in (
            ("analysis_unit_id", analysis_unit_id),
            ("source_analysis_units_config", source_analysis_units_config),
            ("source_analysis_units_config_sha256", source_analysis_units_config_sha256),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"focus.{field} 在 analysis_bbox_cell_center 政策下必須是非空白文字")
        _require(len(source_analysis_units_config_sha256) == 64 and all(character in "0123456789abcdef" for character in source_analysis_units_config_sha256), "focus.source_analysis_units_config_sha256 必須是小寫 SHA-256")
    else:
        for field, value in (
            ("analysis_unit_id", analysis_unit_id),
            ("source_analysis_units_config", source_analysis_units_config),
            ("source_analysis_units_config_sha256", source_analysis_units_config_sha256),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"focus.{field} 若提供必須是非空白文字")

    input_config = raw.get("input")
    if not isinstance(input_config, dict):
        raise ValueError("input 必須是物件")
    # 新版以 years 支援連續或非連續的多年度樣本；保留 year 是為了讓既有 2025 pilot
    # 設定可不改語意地重跑。兩欄若同時存在必須一致，避免「檔名像 2025、實際混入 2024」
    # 這類難以追查的結果。
    years_raw = input_config.get("years")
    if years_raw is None:
        years = (_as_positive_int(input_config.get("year"), "input.year"),)
    else:
        if not isinstance(years_raw, list) or not years_raw:
            raise ValueError("input.years 必須是非空白年份清單")
        years = tuple(_as_positive_int(value, "input.years") for value in years_raw)
        _require(len(years) == len(set(years)), "input.years 不可重複")
        _require(tuple(sorted(years)) == years, "input.years 必須由小到大排序")
        legacy_year = input_config.get("year")
        if legacy_year is not None:
            _require(_as_positive_int(legacy_year, "input.year") == years[-1], "input.year 與 input.years 同時存在時必須等於最後一年")
    months_raw = input_config.get("months")
    if not isinstance(months_raw, list) or not months_raw:
        raise ValueError("input.months 必須是非空白清單")
    months = tuple(_as_positive_int(month, "input.months") for month in months_raw)
    _require(all(1 <= month <= 12 for month in months), "input.months 的月份必須介於 1 到 12")
    _require(len(months) == len(set(months)), "input.months 不可重複")
    required_status = input_config.get("required_status")
    if not isinstance(required_status, str) or not required_status:
        raise ValueError("input.required_status 必須是非空白文字")
    cache_kinds_raw = input_config.get("required_cache_kinds")
    if not isinstance(cache_kinds_raw, list) or not cache_kinds_raw or not all(isinstance(item, str) and item for item in cache_kinds_raw):
        raise ValueError("input.required_cache_kinds 必須是非空白文字清單")
    required_schema_major = _as_positive_int(input_config.get("required_cache_schema_major"), "input.required_cache_schema_major")
    _require(required_schema_major == SURFACE_CACHE_SCHEMA_MAJOR, f"本讀取器只接受 surface cache schema {SURFACE_CACHE_SCHEMA_MAJOR}.x")
    expected_timestep_hours = _as_float(input_config.get("expected_timestep_hours"), "input.expected_timestep_hours")
    maximum_source_gap_hours = _as_float(input_config.get("maximum_source_gap_hours"), "input.maximum_source_gap_hours")
    known_time_axis_repairs = _load_known_time_axis_repairs(input_config, years, months, expected_timestep_hours)

    mask_config = raw.get("mask_and_missing_data")
    if not isinstance(mask_config, dict):
        raise ValueError("mask_and_missing_data 必須是物件")
    _require(mask_config.get("static_ocean_mask") == "mask_static.npy", "目前只允許標準 mask_static.npy 作為靜態海域遮罩")

    svd_config = raw.get("svd")
    if not isinstance(svd_config, dict):
        raise ValueError("svd 必須是物件")
    _require(svd_config.get("variables") == ["u_surface_mps", "v_surface_mps", "eta_m"], "SVD variables 必須固定為 u_surface_mps、v_surface_mps、eta_m")

    parallel_config = raw.get("parallel_execution")
    if not isinstance(parallel_config, dict):
        raise ValueError("parallel_execution 必須是物件")

    figure_config = raw.get("figures")
    if not isinstance(figure_config, dict):
        raise ValueError("figures 必須是物件")
    figure_style = figure_config.get("style", "academic_clean_postproduction_v2")
    _require(figure_style == "academic_clean_postproduction_v2", "figures.style 目前必須是 academic_clean_postproduction_v2")
    figure_formats_raw = figure_config.get("output_formats", ["png"])
    if not isinstance(figure_formats_raw, list) or not figure_formats_raw:
        raise ValueError("figures.output_formats 必須是非空白清單")
    _require(
        all(isinstance(item, str) and item in {"png", "svg"} for item in figure_formats_raw),
        "figures.output_formats 目前只允許 png 與 svg",
    )
    _require(len(figure_formats_raw) == len(set(figure_formats_raw)), "figures.output_formats 不可重複")
    figure_transparent_background = figure_config.get("transparent_background", True)
    _require(isinstance(figure_transparent_background, bool), "figures.transparent_background 必須是布林值")

    return AnalysisConfig(
        raw=raw,
        analysis_label=label,
        focus_id=focus_id,
        focus_name_zh=focus_name,
        approval_status=approval_status,
        domain_id=domain_id,
        bbox=(lon_min, lon_max, lat_min, lat_max),
        anchor_lonlat=(anchor_lon, anchor_lat),
        analysis_unit_id=analysis_unit_id,
        source_analysis_units_config=source_analysis_units_config,
        source_analysis_units_config_sha256=source_analysis_units_config_sha256,
        spatial_mask_policy=spatial_mask_policy,
        years=years,
        months=months,
        required_status=required_status,
        required_cache_kinds=frozenset(cache_kinds_raw),
        expected_timestep_hours=expected_timestep_hours,
        maximum_source_gap_hours=maximum_source_gap_hours,
        known_time_axis_repairs=known_time_axis_repairs,
        minimum_static_ocean_cells=_as_positive_int(mask_config.get("minimum_static_ocean_cells"), "mask_and_missing_data.minimum_static_ocean_cells"),
        minimum_cell_valid_fraction=_as_closed_fraction(mask_config.get("minimum_cell_triplet_valid_fraction"), "mask_and_missing_data.minimum_cell_triplet_valid_fraction"),
        max_interpolation_steps=_as_positive_int(mask_config.get("max_consecutive_missing_timesteps_to_interpolate"), "mask_and_missing_data.max_consecutive_missing_timesteps_to_interpolate", allow_zero=True),
        minimum_retained_time_fraction=_as_closed_fraction(mask_config.get("minimum_retained_time_fraction"), "mask_and_missing_data.minimum_retained_time_fraction"),
        requested_mode_count=_as_positive_int(svd_config.get("requested_mode_count"), "svd.requested_mode_count"),
        minimum_reported_mode_count=_as_positive_int(svd_config.get("minimum_reported_mode_count"), "svd.minimum_reported_mode_count"),
        io_workers=_as_positive_int(parallel_config.get("io_workers"), "parallel_execution.io_workers"),
        linear_algebra_threads=_as_positive_int(parallel_config.get("linear_algebra_threads"), "parallel_execution.linear_algebra_threads"),
        figure_mode_count=_as_positive_int(figure_config.get("mode_count"), "figures.mode_count"),
        max_quiver_arrows_per_axis=_as_positive_int(figure_config.get("max_quiver_arrows_per_axis"), "figures.max_quiver_arrows_per_axis"),
        figure_style=figure_style,
        figure_formats=tuple(figure_formats_raw),
        figure_dpi=_as_positive_int(figure_config.get("raster_dpi", 180), "figures.raster_dpi"),
        figure_transparent_background=figure_transparent_background,
    )


def select_axis_slice(axis: np.ndarray, lower: float, upper: float, axis_name: str) -> tuple[slice, tuple[float, float]]:
    """將地理 bbox 向外 snap 到遞增一維規則格網的連續 slice。

    起訖 cell 中心會向 bbox 外側各保留一格，和前處理專案的 focus manifest 策略一致。這
    可避免研究 anchor 剛好落在 cell 間而被切掉；回傳的 stop 是 Python exclusive index。
    """

    _require(axis.ndim == 1 and axis.size >= 2, f"{axis_name} 軸必須是一維且至少兩格")
    _require(np.all(np.isfinite(axis)) and np.all(np.diff(axis) > 0), f"{axis_name} 軸必須嚴格遞增且有限")
    _require(float(axis[0]) <= lower <= float(axis[-1]) and float(axis[0]) <= upper <= float(axis[-1]), f"focus bbox 超出 {axis_name} 格網範圍")
    start = max(0, int(np.searchsorted(axis, lower, side="right")) - 1)
    stop = min(axis.size, int(np.searchsorted(axis, upper, side="left")) + 1)
    _require(start < stop, f"focus bbox 沒有選到任何 {axis_name} 格點")
    return slice(start, stop), (float(axis[start]), float(axis[stop - 1]))


def _load_grid(surface_root: Path, config: AnalysisConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, slice, slice]:
    """讀取分析單元所屬 flow domain 的靜態格網、讀取小窗與正式幾何遮罩。

    cell area 是 SVD 空間內積的面積權重；mask_static 則只表示標準前處理可用海域。
    `select_axis_slice` 會為 memory-map 小窗向外多取一格，但若設定採
    `analysis_bbox_cell_center`，本函式會另外建立封閉 bbox 的 cell-center 遮罩。這讓
    前處理專案核定的北竿／南竿矩形只以邊界內格點參與 SVD，不會把 I/O 緩衝格誤當
    AOI；舊版 `reader_window` pilot 則保持原先的全小窗相容行為。
    """

    grid_dir = surface_root / config.domain_id / "grid"
    if not grid_dir.is_dir():
        raise FileNotFoundError(f"找不到 surface grid 目錄: {grid_dir}")
    grid_metadata = _read_json_object(grid_dir / "metadata.json")
    metadata_domain = grid_metadata.get("domain", {})
    if isinstance(metadata_domain, dict):
        _require(metadata_domain.get("domain_id") == config.domain_id, "grid metadata domain 與設定不一致")

    def load_grid_array(filename: str) -> np.ndarray:
        path = grid_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"grid 缺少必要檔案: {path}")
        return np.load(path, mmap_mode="r", allow_pickle=False)

    lon = np.asarray(load_grid_array("lon.npy"), dtype=np.float64)
    lat = np.asarray(load_grid_array("lat.npy"), dtype=np.float64)
    area = np.asarray(load_grid_array("cell_area_m2.npy"), dtype=np.float64)
    static = np.asarray(load_grid_array("mask_static.npy"), dtype=bool)
    _require(area.shape == (lat.size, lon.size), "cell_area_m2.npy shape 必須是 (lat, lon)")
    _require(static.shape == area.shape, "mask_static.npy 必須與 cell_area_m2 對齊")
    _require(np.all(np.isfinite(area[static])) and np.all(area[static] > 0), "海域 cell_area_m2 必須為有限正值")

    lon_min, lon_max, lat_min, lat_max = config.bbox
    lon_slice, _ = select_axis_slice(lon, lon_min, lon_max, "longitude")
    lat_slice, _ = select_axis_slice(lat, lat_min, lat_max, "latitude")
    selected_lon = lon[lon_slice]
    selected_lat = lat[lat_slice]
    if config.spatial_mask_policy == "analysis_bbox_cell_center":
        # 以極小容差吸收 JSON 十進位座標與 float64 格點中心轉換的尾數差；它遠小於 1 km
        # 格距，故不會把真正位於邊界外的格點納入 AOI。
        coordinate_tolerance = 1e-10
        lon_inside = (selected_lon >= lon_min - coordinate_tolerance) & (selected_lon <= lon_max + coordinate_tolerance)
        lat_inside = (selected_lat >= lat_min - coordinate_tolerance) & (selected_lat <= lat_max + coordinate_tolerance)
        analysis_geometry_mask = lat_inside[:, None] & lon_inside[None, :]
        _require(np.any(analysis_geometry_mask), "analysis bbox 沒有任何 cell center；請檢查前處理分析單元與 flow grid")
    else:
        # 相容既有單區 candidate pilot：其 bbox 小窗本身就是當時約定的分析範圍。
        analysis_geometry_mask = np.ones((selected_lat.size, selected_lon.size), dtype=bool)
    return selected_lon, selected_lat, area[lat_slice, lon_slice], static[lat_slice, lon_slice], analysis_geometry_mask, lat_slice, lon_slice


def _validate_month_metadata(metadata: dict[str, Any], config: AnalysisConfig, month_id: str, *, allow_partial_months: bool, allow_trial: bool) -> str:
    """驗證一個月份是否可作本次 SVD 的輸入，並回傳其 cache kind。

    預設嚴格接受 `ready` + `standard_month`；partial month 與 trial 都必須由 CLI 明確
    開關授權。即使開放 partial，後續仍會檢查實際 UTC 時間缺口，不能因目錄存在就假定
    為連續逐時資料。
    """

    domain = metadata.get("domain")
    _require(isinstance(domain, dict) and domain.get("domain_id") == config.domain_id, f"{month_id} metadata domain 與設定不一致")
    _require(metadata.get("month") == month_id, f"{month_id} metadata.month 不一致")
    schema = metadata.get("cache_schema_version")
    _require(isinstance(schema, str) and schema.split(".", 1)[0] == str(SURFACE_CACHE_SCHEMA_MAJOR), f"{month_id} cache schema 非 {SURFACE_CACHE_SCHEMA_MAJOR}.x")
    status = metadata.get("status")
    cache_kind = metadata.get("cache_kind")
    if allow_trial and status == "trial_ready" and cache_kind == "trial_partial_month":
        return cache_kind
    _require(status == config.required_status, f"{month_id} status 必須是 {config.required_status}，實際為 {status!r}")
    if cache_kind in config.required_cache_kinds:
        return str(cache_kind)
    if allow_partial_months and cache_kind == "standard_partial_month":
        return str(cache_kind)
    raise ValueError(f"{month_id} cache_kind={cache_kind!r} 不在允許集合；若確認要用缺日月份需加 --allow-partial-months")


def _apply_known_time_axis_repairs(time_utc_ns: np.ndarray, config: AnalysisConfig, month_id: str) -> tuple[np.ndarray, int]:
    """在記憶體副本套用本月份的顯式時間座標修正，回傳時間軸與修正樣本數。

    規則套用前會逐條比對原始區段的第一與最後時刻，也會檢查索引沒有超界。套用後仍要求
    單月時間嚴格遞增；因此設定只能更正已知錯標，不能造成重複、倒序或用時間位移掩蓋
    不明資料。u/v/eta 與 valid mask 的樣本順序完全不動。
    """

    repaired = np.asarray(time_utc_ns, dtype=np.int64).copy()
    repaired_count = 0
    for repair in config.known_time_axis_repairs:
        if repair.month_id != month_id:
            continue
        _require(repair.stop_index <= repaired.size, f"{month_id} 已知時間軸修正區間超出 time 軸長度")
        segment = repaired[repair.start_index : repair.stop_index]
        _require(segment.size > 0, f"{month_id} 已知時間軸修正區間不可為空")
        _require(
            int(segment[0]) == repair.expected_original_start_ns and int(segment[-1]) == repair.expected_original_end_ns,
            f"{month_id} 已知時間軸修正的原始起訖 UTC 不符；上游快取可能已換版，拒絕套用舊規則",
        )
        repaired[repair.start_index : repair.stop_index] = segment + repair.shift_nanoseconds
        repaired_count += int(segment.size)
    _require(np.all(np.diff(repaired) > 0), f"{month_id} 套用已知修正後 time_utc_ns.npy 必須嚴格遞增")
    return repaired, repaired_count


def _load_one_month_focus_chunk(
    *,
    year: int,
    month: int,
    config: AnalysisConfig,
    domain_root: Path,
    lat_slice: slice,
    lon_slice: slice,
    grid_full_lat: int,
    grid_full_lon: int,
    allow_partial_months: bool,
    allow_trial: bool,
) -> MonthFocusChunk:
    """在一個 I/O worker 中讀取並驗證單月的 focus bbox 小窗。

    這個函式只做獨立月份可完成的工作，沒有跨月狀態；因此可安全交給
    `ThreadPoolExecutor` 平行處理。大陣列仍以 memory-map 開啟，實際配置到 RAM 的只有
    `(time, 3, focus_lat, focus_lon)` float64 小窗與同範圍 bool 遮罩，避免平行 worker
    同時 materialize 完整 flow domain。
    """

    month_id = f"{year}{month:02d}"
    month_dir = domain_root / "months" / month_id
    if not month_dir.is_dir():
        raise FileNotFoundError(f"缺少預期月份快取: {month_dir}")
    metadata = _read_json_object(month_dir / "metadata.json")
    cache_kind = _validate_month_metadata(metadata, config, month_id, allow_partial_months=allow_partial_months, allow_trial=allow_trial)

    def open_month_array(filename: str) -> np.ndarray:
        path = month_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"{month_id} 缺少必要 surface 欄位: {path}")
        return np.load(path, mmap_mode="r", allow_pickle=False)

    time = open_month_array("time_utc_ns.npy")
    u = open_month_array("u_surface_mps.npy")
    v = open_month_array("v_surface_mps.npy")
    eta = open_month_array("eta_m.npy")
    valid = open_month_array("valid_mask_surface.npy")
    _require(time.dtype == np.int64 and time.ndim == 1 and time.size > 0, f"{month_id} time_utc_ns.npy 必須是非空 int64 一維軸")
    _require(np.all(np.diff(time) > 0), f"{month_id} time_utc_ns.npy 必須嚴格遞增")
    repaired_time, repaired_time_step_count = _apply_known_time_axis_repairs(time, config, month_id)
    expected_shape = (time.size, grid_full_lat, grid_full_lon)
    for name, array in (("u_surface_mps.npy", u), ("v_surface_mps.npy", v), ("eta_m.npy", eta)):
        _require(array.ndim == 3 and array.shape == expected_shape, f"{month_id} {name} 必須是 (time, lat, lon) 且與 grid 對齊")
        _require(np.issubdtype(array.dtype, np.floating), f"{month_id} {name} 必須是浮點數")
    _require(valid.dtype == np.bool_ and valid.shape == u.shape, f"{month_id} valid_mask_surface.npy 必須是 bool 且與 u 對齊")

    fields = np.stack(
        (
            np.asarray(u[:, lat_slice, lon_slice], dtype=np.float64),
            np.asarray(v[:, lat_slice, lon_slice], dtype=np.float64),
            np.asarray(eta[:, lat_slice, lon_slice], dtype=np.float64),
        ),
        axis=1,
    )
    return MonthFocusChunk(
        year=year,
        month=month,
        source=SourceMonth(month_id, cache_kind, _canonical_json_hash(metadata), metadata),
        time_utc_ns=repaired_time,
        fields=fields,
        valid_surface=np.asarray(valid[:, lat_slice, lon_slice], dtype=bool),
        repaired_time_step_count=repaired_time_step_count,
    )


def load_surface_focus_data(
    surface_root: Path,
    config: AnalysisConfig,
    *,
    allow_partial_months: bool,
    allow_trial: bool,
) -> LoadedSurfaceData:
    """平行以 memory-map 讀取各月 focus bbox 的 u/v/eta/valid/time，不回讀原始資料。

    `io_workers` 最多同時讀取不同月份，適合 SERVER 的快取檔與網路儲存體；每個 worker
    只 materialize bbox 小窗，避免把約 100×150 的完整 flow domain 複製進多份記憶體。
    主執行緒強制依 config 月份排序後再串接，故平行化只縮短 I/O，不改變時間軸與結果。
    """

    lon, lat, area, static, analysis_geometry_mask, lat_slice, lon_slice = _load_grid(surface_root, config)
    domain_root = surface_root / config.domain_id
    # 月份動態陣列必須維持完整 flow grid shape；只在通過契約後才以 focus slice 讀取小窗。
    grid_full_lat = int(np.load(domain_root / "grid" / "lat.npy", mmap_mode="r", allow_pickle=False).size)
    grid_full_lon = int(np.load(domain_root / "grid" / "lon.npy", mmap_mode="r", allow_pickle=False).size)

    # 年份外層、月份內層的明確排序讓 2024+2025 和單年結果都能以可重現 UTC 順序串接。
    year_month_pairs = tuple((year, month) for year in config.years for month in config.months)
    worker_count = min(config.io_workers, len(year_month_pairs))
    worker_arguments = {
        "config": config,
        "domain_root": domain_root,
        "lat_slice": lat_slice,
        "lon_slice": lon_slice,
        "grid_full_lat": grid_full_lat,
        "grid_full_lon": grid_full_lon,
        "allow_partial_months": allow_partial_months,
        "allow_trial": allow_trial,
    }
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocm-svd-io") as executor:
        futures = {
            (year, month): executor.submit(_load_one_month_focus_chunk, year=year, month=month, **worker_arguments)
            for year, month in year_month_pairs
        }
        # 依設定年份、月序取回 future，而非依完成順序 append，確保同一輸入永遠得到相同 time 軸。
        chunks = [futures[(year, month)].result() for year, month in year_month_pairs]

    fields_all = np.concatenate([chunk.fields for chunk in chunks], axis=0)
    valid_all = np.concatenate([chunk.valid_surface for chunk in chunks], axis=0)
    time_all = np.concatenate([chunk.time_utc_ns for chunk in chunks], axis=0)
    _require(np.all(np.diff(time_all) > 0), "跨月份串接後 time_utc_ns 必須嚴格遞增；不可有重複邊界時次")
    return LoadedSurfaceData(
        lon,
        lat,
        area,
        static,
        analysis_geometry_mask,
        fields_all,
        valid_all,
        time_all,
        lat_slice,
        lon_slice,
        tuple(chunk.source for chunk in chunks),
        sum(chunk.repaired_time_step_count for chunk in chunks),
    )


def _validate_source_time_axis(time_utc_ns: np.ndarray, config: AnalysisConfig) -> tuple[float, float]:
    """檢查整段資料的逐時節奏與最大時間缺口，回傳中位時間步與最大缺口（小時）。"""

    _require(time_utc_ns.size >= 2, "SVD 至少需要兩個時間樣本")
    diffs_hours = np.diff(time_utc_ns).astype(np.float64) / 3_600_000_000_000.0
    median_hours = float(np.median(diffs_hours))
    maximum_hours = float(np.max(diffs_hours))
    tolerance = max(0.01, config.expected_timestep_hours * 0.01)
    _require(abs(median_hours - config.expected_timestep_hours) <= tolerance, f"時間軸中位步長 {median_hours:.6g} 小時，與設定逐時步長 {config.expected_timestep_hours:.6g} 不一致")
    _require(maximum_hours <= config.maximum_source_gap_hours, f"最大來源時間缺口 {maximum_hours:.6g} 小時超過設定門檻 {config.maximum_source_gap_hours:.6g} 小時")
    return median_hours, maximum_hours


def _interpolate_short_gaps(values: np.ndarray, max_steps: int) -> tuple[np.ndarray, np.ndarray]:
    """只對有前後有限端點、且連續長度不超過門檻的缺值段做線性時間插補。

    輸入 `(time, component, ocean_cell)` 的三個分量在上一步已被聯合遮罩，任一分量無效時
    三者同時為 NaN。此函式因此只會在同一 cell、同一短時間段內以兩端觀測值插補，絕不
    跨越資料開頭、結尾或長缺口；回傳的布林 mask 可精確指出衍生值的位置。
    """

    _require(values.ndim == 3 and values.shape[1] == len(COMPONENT_NAMES), "插補輸入必須是 (time, 3, ocean_cell)")
    result = values.copy()
    imputed = np.zeros(result.shape, dtype=bool)
    if max_steps == 0:
        return result, imputed
    time_count, component_count, cell_count = result.shape
    for cell_index in range(cell_count):
        missing = ~np.all(np.isfinite(result[:, :, cell_index]), axis=1)
        start = 0
        while start < time_count:
            if not missing[start]:
                start += 1
                continue
            stop = start + 1
            while stop < time_count and missing[stop]:
                stop += 1
            run_length = stop - start
            bounded = start > 0 and stop < time_count
            if bounded and run_length <= max_steps:
                left = result[start - 1, :, cell_index]
                right = result[stop, :, cell_index]
                if np.all(np.isfinite(left)) and np.all(np.isfinite(right)):
                    fractions = (np.arange(1, run_length + 1, dtype=np.float64) / float(run_length + 1))[:, None]
                    result[start:stop, :, cell_index] = left[None, :] + fractions * (right - left)[None, :]
                    imputed[start:stop, :, cell_index] = True
            start = stop
    return result, imputed


def prepare_analysis_data(loaded: LoadedSurfaceData, config: AnalysisConfig) -> PreparedAnalysisData:
    """建立固定 common-valid mask、處理可追溯短缺值並移除仍不完整的時間樣本。

    每個候選海域格點必須先通過分析單元的靜態幾何遮罩與 `mask_static`，有效率分子再
    要求 `valid_mask_surface=True` 且 u/v/eta 三者皆有限；因此 eta 缺值不會被上游 u/v
    遮罩掩蓋，也不會把 I/O 外擴小窗的格點納入。達到年度或兩年門檻的格點才進入
    SVD；短缺值插補後仍有任一格點缺值的整個時次會被移除，避免把 NaN 或 0 放入
    線性代數運算。
    """

    _validate_source_time_axis(loaded.time_utc_ns, config)
    triplet_valid = loaded.valid_surface & np.all(np.isfinite(loaded.fields), axis=1)
    cell_fraction = np.mean(triplet_valid, axis=0, dtype=np.float64)
    _require(loaded.analysis_geometry_mask.shape == loaded.mask_static.shape, "analysis geometry mask 必須與靜態海域遮罩對齊")
    common_mask = loaded.analysis_geometry_mask & loaded.mask_static & (cell_fraction >= config.minimum_cell_valid_fraction)
    common_cell_count = int(np.count_nonzero(common_mask))
    _require(common_cell_count >= config.minimum_static_ocean_cells, f"common-valid 海域格點只有 {common_cell_count}，小於設定門檻 {config.minimum_static_ocean_cells}")

    cell_rows, cell_cols = np.where(common_mask)
    series = loaded.fields[:, :, cell_rows, cell_cols]
    local_valid = triplet_valid[:, cell_rows, cell_cols]
    # 不可讓某一變數的有限值在另一個變數無效時殘留；三分量共同缺值可維持 coupled SVD 的樣本一致性。
    series = np.where(local_valid[:, None, :], series, np.nan)
    interpolated, imputed = _interpolate_short_gaps(series, config.max_interpolation_steps)
    complete_time = np.all(np.isfinite(interpolated), axis=(1, 2))
    retained_time_fraction = float(np.mean(complete_time))
    _require(retained_time_fraction >= config.minimum_retained_time_fraction, f"短缺值處理後僅保留 {retained_time_fraction:.3%} 時次，低於門檻 {config.minimum_retained_time_fraction:.3%}")
    _require(int(np.count_nonzero(complete_time)) >= 2, "完整時間樣本不足以執行 SVD")
    return PreparedAnalysisData(
        time_utc_ns=loaded.time_utc_ns[complete_time],
        series=interpolated[complete_time],
        common_mask=common_mask,
        cell_triplet_valid_fraction=cell_fraction,
        imputed=imputed[complete_time],
        retained_time_fraction=retained_time_fraction,
        initial_time_count=int(loaded.time_utc_ns.size),
        common_ocean_cell_count=common_cell_count,
    )


def _nearest_common_anchor_index(lon: np.ndarray, lat: np.ndarray, common_mask: np.ndarray, anchor_lonlat: tuple[float, float]) -> int:
    """選擇最靠近研究 anchor 的 common-valid 海域格點，供 SVD 模態正負號固定。"""

    rows, cols = np.where(common_mask)
    _require(rows.size > 0, "common-valid mask 沒有海域格點，無法建立 sign convention")
    anchor_lon, anchor_lat = anchor_lonlat
    # 經度方向以 cos(latitude) 調整，避免在此局地尺度把東西距離明顯高估。
    lon_distance = (lon[cols] - anchor_lon) * math.cos(math.radians(anchor_lat))
    lat_distance = lat[rows] - anchor_lat
    return int(np.argmin(lon_distance * lon_distance + lat_distance * lat_distance))


def _resolve_linear_algebra_threads(requested_threads: int) -> int:
    """依可見 CPU 數量收斂設定的 BLAS 執行緒數，避免 SERVER 小節點被過度超額配置。

    `threadpoolctl` 會把此數值套用到 NumPy 背後的 OpenBLAS、MKL 或 Accelerate thread pool。
    這是協方差矩陣乘法與特徵分解的主要加速來源；月檔 I/O worker 在進入此階段前已完全
    結束，因此不會出現 worker 與 BLAS 同時大量搶核心的巢狀平行化。
    """

    available_cpu_count = os.cpu_count() or 1
    return min(requested_threads, available_cpu_count)


def solve_surface_multivariate_svd(
    prepared: PreparedAnalysisData,
    cell_area_m2: np.ndarray,
    config: AnalysisConfig,
    lon: np.ndarray,
    lat: np.ndarray,
) -> SvdSolution:
    """計算 u/v/eta 標準化、面積加權的 SVD，並回復 PC 與可繪圖 loading。

    u/v 使用共同的面積加權均方根（root mean square, RMS），eta 使用獨立的面積加權
    均方根；RMS 是距平振幅的代表尺度，用來讓單位為 m/s 的流速與單位為 m 的 eta 都能
    公平進入同一個 SVD。這是可解釋三變數共同變化的 normalized SVD，不應誤稱為純動能
    covariance 分析。輸入 `prepared.series`
    的形狀是 `(N, 3, P)`，其中三個分量順序固定為 u、v、eta；本函式依下列可追溯的
    線性代數步驟計算：

    1. 對每一個分量與格點沿時間軸扣除平均，取得距平 `a(t, c, p)`。
    2. 用共同 u/v RMS 與 eta RMS 消除量綱與尺度差異，並以每格 `sqrt(area_p)` 加權，
       轉置為空間列、時間欄的 `X_w`，形狀為 `(3P, N)`。
    3. 求空間協方差 `C = X_w X_w^T/(N-1)`，再以對稱矩陣求解器得到
       `C = U Lambda U^T`。因 `lambda_k = sigma_k^2/(N-1)`，`U` 與
       `sigma_k = sqrt(lambda_k * (N-1))` 正是薄型 SVD `X_w = U Sigma V^T` 的空間
       模態與奇異值。
    4. 以 `PC = U^T X_w = Sigma V^T` 回復每個模態的未標準化時間係數；解釋變異率為
       `lambda_k / sum(lambda)`。最後才把空間向量除回面積權重、乘回 RMS，供圖面以
       m/s 與 m 呈現。

    此處特意使用空間協方差 `eigh`，而非 `np.linalg.svd`，因目前表層資料的 `3P` 遠小於
    `N`，可避免建立更大的分解問題，數學結果仍等價。協方差乘法與 `eigh` 透過受控的
    多核心 BLAS 執行，實際執行緒數會限制在設定值與 SERVER 可見 CPU 數量的較小者，並
    寫入 metadata。此函式的輸出是加權標準化座標下的 `spatial_vectors` 與 `pc`；呼叫端
    必須透過 `_solution_physical_loadings` 才能取得有物理單位的空間 loading。
    """

    _require(cell_area_m2.shape == prepared.common_mask.shape, "cell_area_m2 與 common mask 必須對齊")
    areas = np.asarray(cell_area_m2[prepared.common_mask], dtype=np.float64)
    _require(np.all(np.isfinite(areas)) and np.all(areas > 0), "SVD common 海域格點面積必須為有限正值")
    values = prepared.series
    time_count, component_count, cell_count = values.shape
    _require(component_count == len(COMPONENT_NAMES), "SVD 輸入 component 軸必須依序為 u、v、eta")
    # 每一分量、每一格沿時間軸去平均，確保後續模態描述的是相對平均流場的共同變化，
    # 而非全年背景流或平均海面高度；`means` 會另存輸出供重建原始場使用。
    means = np.mean(values, axis=0)
    anomalies = values - means[None, :, :]
    area_total = float(np.sum(areas))
    velocity_rms = math.sqrt(float(np.sum(areas[None, :] * (anomalies[:, 0, :] ** 2 + anomalies[:, 1, :] ** 2)) / (2.0 * time_count * area_total)))
    eta_rms = math.sqrt(float(np.sum(areas[None, :] * anomalies[:, 2, :] ** 2) / (time_count * area_total)))
    _require(np.isfinite(velocity_rms) and velocity_rms > 0.0, "u/v 距平 RMS 為零或非有限，無法做三變數 SVD")
    _require(np.isfinite(eta_rms) and eta_rms > 0.0, "eta 距平 RMS 為零或非有限，無法做三變數 SVD")

    # 狀態向量的欄位順序是全部 u 格點、全部 v 格點、全部 eta 格點。u/v 共用 RMS 以維持
    # 水平向量兩分量的相對大小，eta 獨立標準化以防 m 與 m/s 的數值尺度主導 SVD。
    scaled = np.concatenate((anomalies[:, 0, :] / velocity_rms, anomalies[:, 1, :] / velocity_rms, anomalies[:, 2, :] / eta_rms), axis=1)
    # 面積內積要求每個狀態元素乘 sqrt(area)，所以 X_w.T X_w 與 X_w X_w.T 都對應格點
    # 面積加權，而非讓高緯度或小格點因網格數量較多而不當放大其影響。
    sqrt_weights = np.sqrt(np.tile(areas, len(COMPONENT_NAMES)))
    weighted_state = (scaled * sqrt_weights[None, :]).T
    linear_algebra_threads = _resolve_linear_algebra_threads(config.linear_algebra_threads)
    # matrix multiplication 與特徵分解是本 run 最密集的數值工作；限定 thread pool 可避免
    # SERVER 預設 OpenBLAS/MKL 使用過多核心，影響同一節點上的其他前處理或使用者工作。
    with threadpool_limits(limits=linear_algebra_threads):
        # 這兩行就是本專案實際執行 SVD 的位置：先建立 C = X_w X_w^T/(N-1)，再求
        # C 的特徵對 (lambda, U)。`eigh` 適用於實對稱 C，並比直接完整 SVD 更省資源；
        # 之後以 sigma=sqrt(lambda*(N-1))、PC=U.T@X_w 回復與薄型 SVD 相同的量。
        covariance = (weighted_state @ weighted_state.T) / float(time_count - 1)
        eigenvalues, spatial_vectors = np.linalg.eigh(covariance)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = np.maximum(eigenvalues[order], 0.0)
    all_spatial_vectors = spatial_vectors[:, order]
    total_eigenvalue = float(np.sum(eigenvalues))
    _require(total_eigenvalue > 0.0, "SVD 協方差總變異為零")
    rank_threshold = max(float(eigenvalues[0]) * np.finfo(np.float64).eps * max(weighted_state.shape), np.finfo(np.float64).tiny)
    numerical_rank = int(np.count_nonzero(eigenvalues > rank_threshold))
    mode_count = min(config.requested_mode_count, numerical_rank)
    _require(mode_count >= config.minimum_reported_mode_count, f"可辨識 SVD rank={numerical_rank}，低於至少報告 {config.minimum_reported_mode_count} 模態的需求")
    spatial_vectors = all_spatial_vectors[:, :mode_count].copy()
    # 由 C 的定義可知 lambda=sigma^2/(N-1)；只保留設定要求且數值上可辨識的前幾個模態。
    singular_values = np.sqrt(eigenvalues[:mode_count] * float(time_count - 1))
    with threadpool_limits(limits=linear_algebra_threads):
        # U.T @ X_w 等於薄型 SVD 的 Sigma @ V.T，故每列是該空間模態對應的原始 PC 時序。
        pc = spatial_vectors.T @ weighted_state

    anchor_index = _nearest_common_anchor_index(lon, lat, prepared.common_mask, config.anchor_lonlat)
    sign_sources: list[str] = []
    anchor_rows = (anchor_index, cell_count + anchor_index, 2 * cell_count + anchor_index)
    for mode_index in range(mode_count):
        chosen_index = next((index for index in anchor_rows if abs(float(spatial_vectors[index, mode_index])) > 1e-14), None)
        if chosen_index is None:
            chosen_index = int(np.argmax(np.abs(spatial_vectors[:, mode_index])))
            sign_sources.append("largest_absolute_loading")
        elif chosen_index == anchor_rows[0]:
            sign_sources.append("anchor_u_loading")
        elif chosen_index == anchor_rows[1]:
            sign_sources.append("anchor_v_loading")
        else:
            sign_sources.append("anchor_eta_loading")
        if spatial_vectors[chosen_index, mode_index] < 0:
            spatial_vectors[:, mode_index] *= -1.0
            pc[mode_index, :] *= -1.0

    # 協方差總跡等於總變異；以 lambda 比例定義 explained variance，與 sigma^2 比例相同。
    all_ev = eigenvalues / total_eigenvalue
    explained = all_ev[:mode_count]
    cumulative = np.cumsum(explained)
    orthogonality = spatial_vectors.T @ spatial_vectors - np.eye(mode_count)
    orthogonality_error = float(np.max(np.abs(orthogonality)))
    all_positive_vectors = all_spatial_vectors[:, :numerical_rank]
    with threadpool_limits(limits=linear_algebra_threads):
        full_reconstructed = all_positive_vectors @ (all_positive_vectors.T @ weighted_state)
        full_error = float(np.linalg.norm(weighted_state - full_reconstructed) / np.linalg.norm(weighted_state))
        retained_reconstructed = spatial_vectors @ pc
        retained_error = float(np.linalg.norm(weighted_state - retained_reconstructed) / np.linalg.norm(weighted_state))
    return SvdSolution(
        spatial_vectors=spatial_vectors,
        pc=pc,
        singular_values=singular_values,
        explained_variance=explained,
        cumulative_explained_variance=cumulative,
        all_explained_variance=all_ev,
        mean_by_component=means,
        velocity_rms_mps=velocity_rms,
        eta_rms_m=eta_rms,
        anchor_cell_index=anchor_index,
        sign_sources=tuple(sign_sources),
        full_rank_relative_reconstruction_error=full_error,
        retained_mode_relative_reconstruction_error=retained_error,
        orthogonality_max_abs_error=orthogonality_error,
        linear_algebra_threads=linear_algebra_threads,
    )


def _expand_ocean_values(values: np.ndarray, common_mask: np.ndarray) -> np.ndarray:
    """將 `(mode-or-component, ocean_cell)` 值回填成 `(mode-or-component, lat, lon)`，非分析格點維持 NaN。"""

    _require(values.ndim == 2 and values.shape[1] == int(np.count_nonzero(common_mask)), "回填值必須與 common ocean cell 數量對齊")
    expanded = np.full((values.shape[0], *common_mask.shape), np.nan, dtype=np.float64)
    expanded[:, common_mask] = values
    return expanded


def _solution_physical_loadings(solution: SvdSolution, prepared: PreparedAnalysisData, cell_area_m2: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """將加權標準化 SVD 空間向量轉回 u、v、eta 的物理分量 loading。"""

    areas = np.asarray(cell_area_m2[prepared.common_mask], dtype=np.float64)
    cell_count = areas.size
    inverse_sqrt_weights = 1.0 / np.sqrt(np.tile(areas, len(COMPONENT_NAMES)))
    unweighted = solution.spatial_vectors * inverse_sqrt_weights[:, None]
    physical = unweighted * np.concatenate(
        (
            np.full(cell_count, solution.velocity_rms_mps),
            np.full(cell_count, solution.velocity_rms_mps),
            np.full(cell_count, solution.eta_rms_m),
        )
    )[:, None]
    return (
        _expand_ocean_values(physical[:cell_count, :].T, prepared.common_mask),
        _expand_ocean_values(physical[cell_count : 2 * cell_count, :].T, prepared.common_mask),
        _expand_ocean_values(physical[2 * cell_count :, :].T, prepared.common_mask),
    )


def _academic_visualization_fields(
    solution: SvdSolution,
    mode_u: np.ndarray,
    mode_v: np.ndarray,
    mode_eta: np.ndarray,
) -> AcademicVisualizationFields:
    """把原始 loading/PC 轉成學術圖常用的標準化 PC 與物理回歸模態。

    海洋與氣候 SVD 圖常將 PC 除以自身樣本標準差，再把原場投影或回歸到此標準化 PC，
    讓空間圖保留可解讀的物理單位。對本管線而言，原 `eof_* × pc` 已是單模態重建，
    因此將 `eof_*` 乘上 PC 標準差、PC 除以同一標準差即可得到等價表示。PC 理論平均為
    零；實作仍先移除浮點殘差並把最大殘差記錄到 metadata，避免圖面基線受數值尾差影響。
    """

    pc_mean = np.mean(solution.pc, axis=1)
    pc_centered = solution.pc - pc_mean[:, None]
    pc_standard_deviation = np.std(pc_centered, axis=1, ddof=1)
    _require(
        np.all(np.isfinite(pc_standard_deviation)) and np.all(pc_standard_deviation > 0),
        "PC 樣本標準差必須為有限正值，才能建立學術圖的標準化 PC 與回歸空間模態",
    )
    shape_scale = pc_standard_deviation[:, None, None]
    return AcademicVisualizationFields(
        pc_standardized=pc_centered / pc_standard_deviation[:, None],
        pc_standard_deviation=pc_standard_deviation,
        regression_u=mode_u * shape_scale,
        regression_v=mode_v * shape_scale,
        regression_eta=mode_eta * shape_scale,
        pc_mean_max_abs=float(np.max(np.abs(pc_mean))),
    )


def _iso_utc_from_ns(value: int) -> str:
    """將 UTC epoch 奈秒轉為不含本機時區歧義的 ISO 8601 字串。"""

    return np.datetime_as_string(np.datetime64(int(value), "ns"), unit="s") + "Z"


def _json_safe(value: Any) -> Any:
    """遞迴將 NumPy scalar 與路徑轉成標準 JSON 型別，避免 metadata 出現 NaN 或不可序列化物件。"""

    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8、穩定縮排寫入 metadata 或設定副本，讓人與程式都能檢視 SVD provenance。"""

    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _array_descriptor(array: np.ndarray, dimensions: list[str], unit: str) -> dict[str, Any]:
    """建立 derived `.npy` 的 shape、dtype、維度與單位描述，供下游圖表與審查使用。"""

    return {"shape": [int(item) for item in array.shape], "dtype": str(array.dtype), "dimensions": dimensions, "unit": unit}


def _configured_year_label(config: AnalysisConfig) -> str:
    """將單年或多年的設定年份轉成圖面與 metadata 可讀標籤，不從檔名推測研究期間。"""

    return "+".join(str(year) for year in config.years)


def _make_figures(
    output_dir: Path,
    lon: np.ndarray,
    lat: np.ndarray,
    mean_u: np.ndarray,
    mean_v: np.ndarray,
    mean_eta: np.ndarray,
    visualization: AcademicVisualizationFields,
    time_utc_ns: np.ndarray,
    explained_variance: np.ndarray,
    config: AnalysisConfig,
) -> list[str]:
    """產生無文字、透明背景且適合學術報告後製的 SVD 圖層。

    圖面遵循海洋流場 SVD 文獻常見結構：空間模態以 eta 物理回歸幅度作底色、u/v 回歸
    向量疊圖，並為每個模態另輸出標準化 PC 時序；解釋變異另外輸出 scree/cumulative
    圖。依後製需求，圖內不放標題、座標文字、刻度文字、圖例、色條、anchor 或 EV 註記。
    所有被移除的單位、色階、箭頭尺度、EV 與來源慣例都寫入 `plot_metadata.json`。

    PC 遇到來源缺日會斷線，不能用直線跨越沒有資料的時段。PNG 供快速預覽，SVG 保留箭頭
    與曲線向量結構；透明背景方便後續排版軟體另加海岸線、標籤、panel letter 與色條。
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    step = max(1, int(math.ceil(max(lon.size, lat.size) / config.max_quiver_arrows_per_axis)))
    lon_min, lon_max, lat_min, lat_max = config.bbox
    finite_mean_rows, finite_mean_cols = np.where(np.isfinite(mean_eta))
    _require(finite_mean_rows.size > 0, "平均 eta 沒有任何有限分析格點，無法建立學術圖")
    lon_step = float(np.median(np.diff(lon)))
    lat_step = float(np.median(np.diff(lat)))
    # 分析 bbox 是 cell-center inclusion 契約，但 raster 真正有資料的範圍應到最外側有效
    # cell edge。若 bbox 邊界比最後一格 cell edge 更外，直接畫到 bbox 會留下沒有資料的
    # 透明白帶；因此圖面裁到 bbox 與有效 cell edge 的交集，sidecar 同時保留兩種範圍。
    plot_lon_min = max(lon_min, float(lon[finite_mean_cols].min()) - lon_step / 2.0)
    plot_lon_max = min(lon_max, float(lon[finite_mean_cols].max()) + lon_step / 2.0)
    plot_lat_min = max(lat_min, float(lat[finite_mean_rows].min()) - lat_step / 2.0)
    plot_lat_max = min(lat_max, float(lat[finite_mean_rows].max()) + lat_step / 2.0)
    lon_span = plot_lon_max - plot_lon_min
    lat_span = plot_lat_max - plot_lat_min
    geographic_aspect = 1.0 / math.cos(math.radians((lat_min + lat_max) / 2.0))
    asset_metadata: dict[str, Any] = {"modes": []}

    def save_clean_figure(fig: Any, stem: str) -> list[str]:
        """依設定輸出同一無文字圖層的 PNG/SVG，回傳相對於 run 目錄的路徑。

        `bbox_inches=tight` 與零 padding 會移除後製不需要的空白；SVG 不套 DPI，PNG 則以
        設定解析度輸出。透明選項同時作用於 figure 與 axes patch，不會留下白色矩形底。
        """

        paths: list[str] = []
        for output_format in config.figure_formats:
            path = figure_dir / f"{stem}.{output_format}"
            save_kwargs: dict[str, Any] = {
                "bbox_inches": "tight",
                "pad_inches": 0,
                "transparent": config.figure_transparent_background,
            }
            if output_format == "png":
                save_kwargs["dpi"] = config.figure_dpi
            fig.savefig(path, **save_kwargs)
            relative_path = str(path.relative_to(output_dir))
            created.append(relative_path)
            paths.append(relative_path)
        return paths

    def finite_range(field: np.ndarray) -> tuple[float, float]:
        """取得有限值色階；常數場以極小對稱寬度避免 Matplotlib 出現奇異 normalization。"""

        finite = np.asarray(field[np.isfinite(field)], dtype=np.float64)
        _require(finite.size > 0, "乾淨學術圖的底色欄位至少必須有一個有限值")
        lower = float(np.min(finite))
        upper = float(np.max(finite))
        if lower == upper:
            padding = max(abs(lower) * 1e-6, np.finfo(np.float64).eps)
            lower -= padding
            upper += padding
        return lower, upper

    def symmetric_limit(field: np.ndarray) -> float:
        """回傳以零為中心的有限絕對最大值，確保正負回歸幅度使用對稱色階。"""

        finite = np.asarray(field[np.isfinite(field)], dtype=np.float64)
        _require(finite.size > 0, "SVD 回歸 eta 空間模態至少必須有一個有限值")
        limit = float(np.max(np.abs(finite)))
        return max(limit, np.finfo(np.float64).eps)

    def vector_scale(u_field: np.ndarray, v_field: np.ndarray) -> tuple[float, float]:
        """以有效向量的第 95 百分位決定清楚但不溢出的箭頭尺度。

        地圖寬度約 4.5% 作為第 95 百分位向量的顯示長度；比最大值更不易被單一離群箭頭
        壓縮。回傳物理參考量與 Matplotlib `scale_units="xy"` 所需 scale，兩者都寫入
        sidecar，後製時可建立正式 quiver key。
        """

        magnitude = np.hypot(u_field, v_field)
        finite = np.asarray(magnitude[np.isfinite(magnitude) & (magnitude > 0)], dtype=np.float64)
        _require(finite.size > 0, "流速向量圖至少必須有一個有限非零向量")
        reference = float(np.percentile(finite, 95.0))
        scale = reference / (0.045 * lon_span)
        return reference, scale

    def add_vector_map(
        stem: str,
        color_field: np.ndarray,
        u_field: np.ndarray,
        v_field: np.ndarray,
        *,
        cmap: str,
        color_limits: tuple[float, float],
    ) -> tuple[list[str], float, float]:
        """輸出無任何文字的標量底色與向量疊圖，並回傳檔案與箭頭尺度。"""

        reference, scale = vector_scale(u_field, v_field)
        figure_width = 6.4
        figure_height = figure_width * (lat_span / lon_span) * geographic_aspect
        fig, axis = plt.subplots(figsize=(figure_width, figure_height))
        fig.patch.set_alpha(0.0 if config.figure_transparent_background else 1.0)
        axis.set_facecolor("none" if config.figure_transparent_background else "white")
        axis.pcolormesh(
            lon_grid,
            lat_grid,
            np.ma.masked_invalid(color_field),
            shading="auto",
            cmap=cmap,
            vmin=color_limits[0],
            vmax=color_limits[1],
        )
        lon_indices = np.arange(0, lon.size, step)
        lat_indices = np.arange(0, lat.size, step)
        quiver_lon = lon_grid[np.ix_(lat_indices, lon_indices)]
        quiver_lat = lat_grid[np.ix_(lat_indices, lon_indices)]
        quiver_u = u_field[np.ix_(lat_indices, lon_indices)]
        quiver_v = v_field[np.ix_(lat_indices, lon_indices)]
        # 最長箭頭可能略大於第 95 百分位；排除距四邊 6% 內的 anchor，可避免箭頭被 clip，
        # 也保留足夠內部向量呈現環流結構。底色仍完整覆蓋全部有效 cell。
        edge_margin_lon = lon_span * 0.06
        edge_margin_lat = lat_span * 0.06
        interior = (
            (quiver_lon >= plot_lon_min + edge_margin_lon)
            & (quiver_lon <= plot_lon_max - edge_margin_lon)
            & (quiver_lat >= plot_lat_min + edge_margin_lat)
            & (quiver_lat <= plot_lat_max - edge_margin_lat)
            & np.isfinite(quiver_u)
            & np.isfinite(quiver_v)
        )
        axis.quiver(
            quiver_lon,
            quiver_lat,
            np.ma.masked_where(~interior, quiver_u),
            np.ma.masked_where(~interior, quiver_v),
            color="black",
            angles="xy",
            scale_units="xy",
            scale=scale,
            pivot="mid",
            width=0.0032,
            headwidth=3.2,
            headlength=4.2,
            headaxislength=3.8,
        )
        axis.set_xlim(plot_lon_min, plot_lon_max)
        axis.set_ylim(plot_lat_min, plot_lat_max)
        axis.set_aspect(geographic_aspect, adjustable="box")
        axis.set_axis_off()
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        paths = save_clean_figure(fig, stem)
        plt.close(fig)
        return paths, reference, scale

    mean_color_limits = finite_range(mean_eta)
    mean_paths, mean_vector_reference, mean_quiver_scale = add_vector_map(
        "mean_surface_flow_clean",
        mean_eta,
        mean_u,
        mean_v,
        cmap="viridis",
        color_limits=mean_color_limits,
    )
    asset_metadata["mean_surface_flow"] = {
        "files": mean_paths,
        "eta_color_map": "viridis",
        "eta_color_limits_m": list(mean_color_limits),
        "vector_reference_mps_at_95th_percentile": mean_vector_reference,
        "matplotlib_quiver_scale": mean_quiver_scale,
    }

    time_datetime = time_utc_ns.astype("datetime64[ns]")
    diffs_hours = np.diff(time_utc_ns).astype(np.float64) / 3_600_000_000_000.0
    gap_after_indices = np.where(diffs_hours > config.expected_timestep_hours * 1.5)[0]
    segment_starts = np.concatenate((np.array([0], dtype=int), gap_after_indices + 1))
    segment_stops = np.concatenate((gap_after_indices + 1, np.array([time_utc_ns.size], dtype=int)))
    time_days = time_datetime.astype("datetime64[D]")
    unique_days, day_inverse = np.unique(time_days, return_inverse=True)
    daily_gap_after_indices = np.where(np.diff(unique_days).astype("timedelta64[D]").astype(np.int64) > 1)[0]
    daily_segment_starts = np.concatenate((np.array([0], dtype=int), daily_gap_after_indices + 1))
    daily_segment_stops = np.concatenate((daily_gap_after_indices + 1, np.array([unique_days.size], dtype=int)))
    figure_mode_count = min(config.figure_mode_count, visualization.regression_u.shape[0])
    for mode_index in range(figure_mode_count):
        mode_number = mode_index + 1
        eta_limit = symmetric_limit(visualization.regression_eta[mode_index])
        spatial_paths, vector_reference, quiver_scale = add_vector_map(
            f"svd_mode_{mode_number:02d}_spatial_clean",
            visualization.regression_eta[mode_index],
            visualization.regression_u[mode_index],
            visualization.regression_v[mode_index],
            cmap="RdBu_r",
            color_limits=(-eta_limit, eta_limit),
        )

        # 淡灰逐時線保留原始高頻結構，黑色日平均線提供全年尺度可讀輪廓；這對應海洋流場
        # SVD 論文常見的 raw + smoothed time mode 疊圖。兩層都分段繪製，缺日不會被連線。
        pc_values = visualization.pc_standardized[mode_index]
        daily_values = np.array(
            [float(np.mean(pc_values[day_inverse == day_index])) for day_index in range(unique_days.size)],
            dtype=np.float64,
        )
        pc_limit = max(float(np.max(np.abs(pc_values))), 1.0)
        fig, axis = plt.subplots(figsize=(10.0, 2.8))
        fig.patch.set_alpha(0.0 if config.figure_transparent_background else 1.0)
        axis.set_facecolor("none" if config.figure_transparent_background else "white")
        axis.axhline(0.0, color="#808080", linewidth=0.55, alpha=0.8)
        for start, stop in zip(segment_starts, segment_stops, strict=True):
            axis.plot(time_datetime[start:stop], pc_values[start:stop], color="#6F6F6F", linewidth=0.32, alpha=0.24)
        for start, stop in zip(daily_segment_starts, daily_segment_stops, strict=True):
            axis.plot(unique_days[start:stop], daily_values[start:stop], color="black", linewidth=1.05)
        axis.set_xlim(time_datetime[0], time_datetime[-1])
        axis.set_ylim(-pc_limit * 1.04, pc_limit * 1.04)
        axis.set_axis_off()
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        pc_paths = save_clean_figure(fig, f"svd_mode_{mode_number:02d}_pc_clean")
        plt.close(fig)
        asset_metadata["modes"].append(
            {
                "mode": mode_number,
                "explained_variance_fraction": float(explained_variance[mode_index]),
                "spatial_files": spatial_paths,
                "pc_files": pc_paths,
                "eta_color_map": "RdBu_r",
                "eta_symmetric_color_limit_m_per_pc_standard_deviation": eta_limit,
                "vector_reference_mps_per_pc_standard_deviation_at_95th_percentile": vector_reference,
                "matplotlib_quiver_scale": quiver_scale,
                "pc_y_symmetric_limit_standard_deviation": pc_limit * 1.04,
                "pc_display_layers": ["hourly standardized PC in translucent gray", "daily mean standardized PC in black"],
            }
        )

    fig, axis = plt.subplots(figsize=(8.0, 4.2))
    fig.patch.set_alpha(0.0 if config.figure_transparent_background else 1.0)
    axis.set_facecolor("none" if config.figure_transparent_background else "white")
    mode_numbers = np.arange(1, explained_variance.size + 1)
    axis.bar(mode_numbers, explained_variance * 100.0, width=0.76, color="#2A6F97")
    axis.plot(mode_numbers, np.cumsum(explained_variance) * 100.0, color="black", linewidth=1.2, marker="o", markersize=3.4)
    axis.axhline(0.0, color="#808080", linewidth=0.55)
    axis.set_xlim(0.45, float(mode_numbers[-1]) + 0.55)
    axis.set_ylim(0.0, 100.0)
    axis.set_axis_off()
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    explained_paths = save_clean_figure(fig, "svd_explained_variance_clean")
    plt.close(fig)
    asset_metadata["explained_variance"] = {
        "files": explained_paths,
        "individual_fraction": explained_variance.tolist(),
        "cumulative_fraction": np.cumsum(explained_variance).tolist(),
    }

    plot_metadata = {
        "schema_name": "ocm_svd_academic_clean_figure_assets",
        "schema_version": "1.0.0",
        "style": config.figure_style,
        "text_policy": {
            "contains_text": False,
            "removed_elements": [
                "title",
                "axis labels",
                "tick labels",
                "legend",
                "color bar",
                "focus anchor",
                "panel letters",
                "mode and explained-variance annotations",
            ],
            "purpose": "保留純資料圖層，供報告或論文排版軟體後製。",
        },
        "rendering": {
            "formats": list(config.figure_formats),
            "png_dpi": config.figure_dpi,
            "transparent_background": config.figure_transparent_background,
            "analysis_bbox_lon_lat": list(config.bbox),
            "plotted_valid_cell_edge_bbox_lon_lat": [plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max],
            "quiver_grid_stride": step,
        },
        "spatial_pattern_representation": {
            "pc": "每一模態以樣本標準差 ddof=1 標準化為無因次 PC。",
            "scalar_background": "eta 回歸空間模態，單位 m per 1 standard deviation of PC。",
            "vectors": "u/v 回歸空間模態，單位 m s-1 per 1 standard deviation of PC。",
            "equivalence": "regression_pattern × standardized_PC 等價於原 physical_loading × raw_PC 的單模態距平重建（忽略記錄於 metadata 的浮點 PC 均值尾差）。",
        },
        "time_series": {
            "unit": "standard deviation",
            "gap_policy": "相鄰樣本間隔超過 1.5 倍設定時間步即斷線，不跨缺日連線。",
            "gap_break_count": int(gap_after_indices.size),
            "display_layers": {
                "hourly": "透明灰線，保留原始高頻 PC。",
                "daily_mean": "黑線；只作顯示用同日算術平均，不改變或取代輸出的逐時 pc_standardized.npy。",
            },
            "daily_gap_break_count": int(daily_gap_after_indices.size),
        },
        "academic_visual_references": [
            {
                "doi": "10.5194/os-18-1183-2022",
                "applied_convention": "海表流 SVD 空間向量圖與對應時間模態分開呈現，向量抽稀以維持可讀性。",
            },
            {
                "doi": "10.5194/os-21-3361-2025",
                "applied_convention": "SVD 以標量底色疊加流速向量，並另列對應 PC 時序。",
            },
            {
                "doi": "10.5194/os-18-1741-2022",
                "applied_convention": "PC 標準化後，空間圖以每 1 個 PC 標準差對應的物理回歸幅度呈現。",
            },
        ],
        "assets": asset_metadata,
    }
    plot_metadata_path = figure_dir / "plot_metadata.json"
    _write_json(plot_metadata_path, plot_metadata)
    created.append(str(plot_metadata_path.relative_to(output_dir)))
    return created


def _create_run_id(config: AnalysisConfig, source_months: tuple[SourceMonth, ...]) -> str:
    """以設定與所有月份 metadata hash 建立穩定 run ID，不把私有 SERVER 路徑放入名稱。"""

    source_signature = [{"month": item.month_id, "metadata_sha256": item.metadata_sha256} for item in source_months]
    digest = _canonical_json_hash({"config": config.raw, "source_months": source_signature})[:12]
    safe_label = "".join(character if character.isalnum() or character in "-_" else "_" for character in config.analysis_label)
    return f"{safe_label}_{digest}"


def run_surface_multivariate_svd(
    *,
    config_path: Path,
    surface_root: Path,
    output_root: Path,
    allow_partial_months: bool = False,
    allow_trial: bool = False,
    make_figures: bool = True,
) -> Path:
    """執行一個只讀 surface cache 的三變數表層 SVD run，並原子發布成果目錄。

    這是 CLI 與測試共用的高階入口。它先驗證設定列出的所有年份／月份、時間軸與 common-valid mask，
    再求解 SVD；若任何檢查失敗，最終目錄不會被建立。focus 為 candidate 時，即使輸入
    cache 已 ready，metadata 仍固定標記 `candidate_pilot`，防止成果被誤用為核定 AOI 結論。
    """

    performance = PerformanceRecorder()
    # 把設定解析與輸出目標檢查獨立計時，六區批次若在此耗時或失敗，可直接判定是設定／
    # 檔案系統問題，而不會誤認為 SVD 求解器變慢。
    with performance.measure("configuration_and_output_validation"):
        config = load_analysis_config(config_path)
        surface_root = surface_root.resolve()
        output_root = output_root.resolve()
        _require(surface_root.is_dir(), f"surface_root 不存在或不是目錄: {surface_root}")

    # 月資料讀取和缺值準備分開量測：前者主要反映共享磁碟與 memory-map I/O，後者則反映
    # bbox 大小、有效遮罩與短缺值掃描成本，對六區同時執行時的瓶頸判讀很重要。
    with performance.measure("surface_focus_month_io"):
        loaded = load_surface_focus_data(surface_root, config, allow_partial_months=allow_partial_months, allow_trial=allow_trial)
    with performance.measure("mask_and_missing_data_preparation"):
        prepared = prepare_analysis_data(loaded, config)
    with performance.measure("svd_solver"):
        solution = solve_surface_multivariate_svd(prepared, loaded.cell_area_m2, config, loaded.lon, loaded.lat)
    with performance.measure("physical_and_visualization_field_derivation"):
        mode_u, mode_v, mode_eta = _solution_physical_loadings(solution, prepared, loaded.cell_area_m2)
        visualization = _academic_visualization_fields(solution, mode_u, mode_v, mode_eta)
        mean_fields = _expand_ocean_values(solution.mean_by_component, prepared.common_mask)
        mean_u, mean_v, mean_eta = mean_fields[0], mean_fields[1], mean_fields[2]
        run_id = _create_run_id(config, loaded.source_months)
        final_dir = output_root / "svd" / run_id
        if final_dir.exists():
            raise FileExistsError(f"SVD 成果已存在，為保護可重現性拒絕覆寫: {final_dir}")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        partial_dir = final_dir.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
        partial_dir.mkdir(parents=False)

    try:
        # 輸出格網與資料矩陣的必要 sidecar；所有 array 都以 allow_pickle=False 可讀格式保存。
        with performance.measure("array_and_provenance_serialization"):
            arrays_to_write: dict[str, np.ndarray] = {
                "lon.npy": loaded.lon,
                "lat.npy": loaded.lat,
                "cell_area_m2.npy": loaded.cell_area_m2,
                "analysis_geometry_mask.npy": loaded.analysis_geometry_mask,
                "valid_mask.npy": prepared.common_mask,
                "cell_triplet_valid_fraction.npy": prepared.cell_triplet_valid_fraction,
                "time_utc_ns.npy": prepared.time_utc_ns,
                "imputed_mask.npy": _expand_imputed_mask(prepared.imputed, prepared.common_mask),
                "mean_u.npy": mean_u,
                "mean_v.npy": mean_v,
                "mean_eta.npy": mean_eta,
                "mode_u.npy": mode_u,
                "mode_v.npy": mode_v,
                "mode_eta.npy": mode_eta,
                "pc.npy": solution.pc,
                "pc_standardized.npy": visualization.pc_standardized,
                "regression_u.npy": visualization.regression_u,
                "regression_v.npy": visualization.regression_v,
                "regression_eta.npy": visualization.regression_eta,
                "singular_values.npy": solution.singular_values,
                "explained_variance.npy": solution.explained_variance,
                "cumulative_explained_variance.npy": solution.cumulative_explained_variance,
                "all_explained_variance.npy": solution.all_explained_variance,
            }
            for filename, array in arrays_to_write.items():
                np.save(partial_dir / filename, array, allow_pickle=False)
            _write_json(partial_dir / "config.json", config.raw)
            spatial_slice = {
                "lat_slice": [loaded.lat_slice.start, loaded.lat_slice.stop],
                "lon_slice": [loaded.lon_slice.start, loaded.lon_slice.stop],
                "slice_stop_is_exclusive": True,
                "grid_center_bbox_lon_lat": [float(loaded.lon[0]), float(loaded.lon[-1]), float(loaded.lat[0]), float(loaded.lat[-1])],
            }
            _write_json(partial_dir / "spatial_slice.json", spatial_slice)
        with performance.measure("figure_rendering"):
            figure_files = (
                _make_figures(
                    partial_dir,
                    loaded.lon,
                    loaded.lat,
                    mean_u,
                    mean_v,
                    mean_eta,
                    visualization,
                    prepared.time_utc_ns,
                    solution.explained_variance,
                    config,
                )
                if make_figures
                else []
            )
        status = "candidate_pilot" if config.approval_status == "candidate" else "analysis_ready"
        if any(source.cache_kind == "trial_partial_month" for source in loaded.source_months):
            status = "trial_pilot"
        arrays_metadata = {
            "mode_u.npy": _array_descriptor(mode_u, ["mode", "lat", "lon"], "m s-1 per PC unit"),
            "mode_v.npy": _array_descriptor(mode_v, ["mode", "lat", "lon"], "m s-1 per PC unit"),
            "mode_eta.npy": _array_descriptor(mode_eta, ["mode", "lat", "lon"], "m per PC unit"),
            "pc.npy": _array_descriptor(solution.pc, ["mode", "time"], "weighted standardized PC"),
            "pc_standardized.npy": _array_descriptor(visualization.pc_standardized, ["mode", "time"], "dimensionless standard deviations; each mode uses sample std ddof=1"),
            "regression_u.npy": _array_descriptor(visualization.regression_u, ["mode", "lat", "lon"], "m s-1 per 1 standard deviation of corresponding PC"),
            "regression_v.npy": _array_descriptor(visualization.regression_v, ["mode", "lat", "lon"], "m s-1 per 1 standard deviation of corresponding PC"),
            "regression_eta.npy": _array_descriptor(visualization.regression_eta, ["mode", "lat", "lon"], "m per 1 standard deviation of corresponding PC"),
            "valid_mask.npy": _array_descriptor(prepared.common_mask, ["lat", "lon"], "bool common-valid analysis ocean mask"),
            "analysis_geometry_mask.npy": _array_descriptor(loaded.analysis_geometry_mask, ["lat", "lon"], "bool; cell centers admitted by the versioned analysis boundary before ocean and dynamic-valid filtering"),
            "imputed_mask.npy": _array_descriptor(arrays_to_write["imputed_mask.npy"], ["time", "component(u,v,eta)", "lat", "lon"], "bool; true only where short-gap interpolation was applied"),
            "time_utc_ns.npy": _array_descriptor(prepared.time_utc_ns, ["time"], "UTC epoch nanoseconds"),
        }
        # 候選區與核定區的科學限制不同：前四區不可被誤用為正式 AOI 結論；北竿／南竿則
        # 已核定分析範圍，但仍受此 run 的年份、表層變數與資料品質門檻限制。
        approval_limitation = (
            "focus approval_status=candidate；此成果只能作候選區 SVD pilot，不可取代研究團隊核定 AOI polygon/cell fraction。"
            if config.approval_status == "candidate"
            else "此成果使用前處理專案版本化核定分析區；任何 AOI 邊界、cell fraction 或 coverage 規則變更都必須建立新的 analysis unit version 與新的 SVD run。"
        )
        time_axis_repair_limitation = (
            f"本 run 依設定中的已知來源證據，只在分析記憶體內修正 {loaded.repaired_time_step_count} 筆 UTC 時間座標；"
            "未覆寫上游快取或改動 u/v/eta/valid 數值。規則詳見 input_surface.known_time_axis_repairs。"
            if loaded.repaired_time_step_count
            else "本 run 未套用任何已知時間軸修正。"
        )
        metadata = {
            "schema_name": "ocm_surface_multivariate_svd",
            "schema_version": CONFIG_SCHEMA_VERSION,
            "status": status,
            "run_id": run_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "analysis_kind": "surface_multivariate_svd",
            "analysis_label": config.analysis_label,
            "focus": config.raw["focus"],
            "analysis_unit": {
                "analysis_unit_id": config.analysis_unit_id,
                "source_analysis_units_config": config.source_analysis_units_config,
                "source_analysis_units_config_sha256": config.source_analysis_units_config_sha256,
                "spatial_mask_policy": config.spatial_mask_policy,
                "analysis_bbox_lon_lat": list(config.bbox),
                "geometry_inclusion": "closed_bbox_cell_center" if config.spatial_mask_policy == "analysis_bbox_cell_center" else "reader_window_legacy_compatibility",
                "geometry_cell_center_count_before_static_mask": int(np.count_nonzero(loaded.analysis_geometry_mask)),
            },
            "time_window": {
                "initial_time_count": prepared.initial_time_count,
                "retained_time_count": int(prepared.time_utc_ns.size),
                "retained_time_fraction": prepared.retained_time_fraction,
                "start_utc": _iso_utc_from_ns(int(prepared.time_utc_ns[0])),
                "end_utc": _iso_utc_from_ns(int(prepared.time_utc_ns[-1])),
                "time_encoding": "UTC epoch nanoseconds int64",
            },
            "input_surface": {
                "surface_root": "injected_at_runtime_not_persisted",
                "flow_domain_id": config.domain_id,
                "source_months": [
                    {"month": source.month_id, "cache_kind": source.cache_kind, "metadata_sha256": source.metadata_sha256}
                    for source in loaded.source_months
                ],
                "known_time_axis_repairs": config.raw["input"].get("known_time_axis_repairs", []),
                "repaired_time_step_count": loaded.repaired_time_step_count,
                "raw_netcdf_read": False,
                "materialized_flow_scope": "analysis bbox read window only; cell-center geometry mask excludes any I/O buffer cells from SVD",
            },
            "parallel_execution": {
                "io_workers_configured": config.io_workers,
                "io_workers_used": min(config.io_workers, len(config.years) * len(config.months)),
                "io_policy": "independent year-month analysis-window memory-map reads run concurrently; results are concatenated in configured year then month order",
                "linear_algebra_threads_configured": config.linear_algebra_threads,
                "linear_algebra_threads_used": solution.linear_algebra_threads,
                "linear_algebra_policy": "threadpoolctl limits NumPy BLAS during covariance multiplication, eigendecomposition, PC projection, and reconstruction checks; it starts after I/O workers finish",
            },
            "spatial_slice": spatial_slice,
            "mask_and_missing_data": {
                "common_ocean_cell_count": prepared.common_ocean_cell_count,
                "analysis_geometry_cell_center_count_before_static_mask": int(np.count_nonzero(loaded.analysis_geometry_mask)),
                "minimum_cell_triplet_valid_fraction": config.minimum_cell_valid_fraction,
                "triplet_valid_definition": "valid_mask_surface AND finite(u_surface_mps) AND finite(v_surface_mps) AND finite(eta_m)",
                "short_gap_interpolation_maximum_timesteps": config.max_interpolation_steps,
                "imputed_scalar_count": int(np.count_nonzero(prepared.imputed)),
                "no_zero_fill": True,
            },
            "svd": {
                "solver": "spatial_covariance_eigh_equivalent_to_thin_svd",
                "equation": "X_w X_w^T/(N-1) = U Lambda U^T; singular_values=sqrt((N-1)*Lambda); PC=U^T X_w",
                "state_vector_order": "[u_1..u_P, v_1..v_P, eta_1..eta_P]",
                "area_weight": "sqrt(cell_area_m2), repeated for u, v, eta",
                "anomaly_reference": config.raw["svd"]["anomaly_reference"],
                "normalization": config.raw["svd"]["normalization"],
                "linear_algebra_threads": solution.linear_algebra_threads,
                "velocity_rms_mps": solution.velocity_rms_mps,
                "eta_rms_m": solution.eta_rms_m,
                "mode_count": int(solution.explained_variance.size),
                "minimum_reported_mode_count": config.minimum_reported_mode_count,
                "sign_convention": config.raw["svd"]["sign_convention"],
                "anchor_common_cell_index": solution.anchor_cell_index,
                "sign_sources": list(solution.sign_sources),
                "academic_visualization_representation": {
                    "pc": "pc_standardized=(pc-mean(pc))/sample_std(pc, ddof=1)",
                    "spatial_regression_patterns": "regression_component=mode_component*sample_std(pc, ddof=1)",
                    "single_mode_reconstruction": "regression_component * pc_standardized; physical units match the component anomaly",
                    "pc_standard_deviation_raw_units": visualization.pc_standard_deviation.tolist(),
                    "pc_mean_max_abs_raw_units": visualization.pc_mean_max_abs,
                },
            },
            "quality_checks": {
                "sum_all_explained_variance": float(np.sum(solution.all_explained_variance)),
                "spatial_vector_orthogonality_max_abs_error": solution.orthogonality_max_abs_error,
                "full_rank_relative_reconstruction_error": solution.full_rank_relative_reconstruction_error,
                "retained_mode_relative_reconstruction_error": solution.retained_mode_relative_reconstruction_error,
            },
            "arrays": arrays_metadata,
            "figures": figure_files,
            "performance": performance.to_metadata(
                scope_end=(
                    "從單區 run 函式入口至 metadata 內容組裝；不含最後 metadata.json 寫入與"
                    "原子目錄 rename。六區 batch 的 per-region elapsed_seconds 會包含這兩步。"
                )
            ),
            "limitations": [
                approval_limitation,
                f"本 run 是 {_configured_year_label(config)} 年表層 u/v/eta 結果；尚未代表未納入年份、固定深度、HAB 或完整三維 SVD。",
                time_axis_repair_limitation,
                "本結果未執行低通、季節分窗、年份比較或 block bootstrap；這些需以獨立設定 run 完成。",
            ],
        }
        _write_json(partial_dir / "metadata.json", metadata)
        os.replace(partial_dir, final_dir)
    except Exception:
        # 只清除本函式剛建立、名稱含 UUID 的 partial run；不碰任何既有成果或上游 surface cache。
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise
    return final_dir


def _expand_imputed_mask(imputed: np.ndarray, common_mask: np.ndarray) -> np.ndarray:
    """將緊湊 `(time, component, ocean_cell)` 插補旗標還原成可與輸出格網對齊的四維 mask。"""

    _require(imputed.ndim == 3 and imputed.shape[1] == len(COMPONENT_NAMES), "imputed mask 必須是 (time, 3, ocean_cell)")
    _require(imputed.shape[2] == int(np.count_nonzero(common_mask)), "imputed mask 與 common mask 海域格點數不一致")
    expanded = np.zeros((imputed.shape[0], imputed.shape[1], *common_mask.shape), dtype=bool)
    expanded[:, :, common_mask] = imputed
    return expanded
