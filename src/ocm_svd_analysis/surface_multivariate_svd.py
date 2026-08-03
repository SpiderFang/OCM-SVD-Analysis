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
from typing import Any, Iterable

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
class TimeAxisCanonicalization:
    """跨月份來源 UTC 軸的可追溯排序與去重摘要。

    全部可得資料的原始檔案若在跨日／跨夜邊界出現倒序或重複 UTC，`sort_and_deduplicate_prefer_last`
    會先按 UTC 穩定排序，再只保留同一 UTC 在原始月份序列中最後出現的完整樣本索引。這不能
    還原未知的真實觀測時刻，而是建立供統計 SVD 使用、每個 UTC 至多一筆的決定性時間座標；
    被重排與捨棄的數量必須寫入 metadata，防止分析結果被誤稱為未經時間整理的原始序列。
    """

    policy: str
    input_time_count: int
    output_time_count: int
    reordered_time_step_count: int
    dropped_duplicate_time_step_count: int


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
    maximum_source_gap_hours: float | None
    known_time_axis_repairs: tuple[TimeAxisRepair, ...]
    time_axis_canonicalization_policy: str
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
    figure_land_overlay_path: Path
    figure_land_overlay_logical_path: str
    figure_land_overlay_sha256: str


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
    time_axis_canonicalization: TimeAxisCanonicalization


@dataclass(frozen=True)
class PreparedAnalysisData:
    """可進入 SVD 的固定空間遮罩、時間樣本與短缺值處理結果。

    `series` 的維度為 `(retained_time, component, ocean_cell)`；這個緊湊表示法避免把陸地
    與未選定的 bbox cell 放進矩陣。`imputed` 僅標記依法插補的短缺值，方便日後將其排除
    做不補值敏感度分析。三個 `source_*` 欄位描述插補前、跨月份串接後的來源 UTC 軸；即使
    全部可得設定解除最大缺口上限，也能在發布 metadata 中量化並揭露實際時間斷點。
    """

    time_utc_ns: np.ndarray
    series: np.ndarray
    common_mask: np.ndarray
    cell_triplet_valid_fraction: np.ndarray
    imputed: np.ndarray
    retained_time_fraction: float
    initial_time_count: int
    common_ocean_cell_count: int
    source_median_timestep_hours: float
    source_maximum_gap_hours: float
    source_gap_break_count: int


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


def _sha256_file_content(path: Path) -> str:
    """串流計算外部資料檔 SHA-256，避免一次把大型岸線 GeoJSON 再複製到記憶體。

    此雜湊用來鎖定報告圖採用的岸線版本；它只驗證圖資位元內容，不代表岸線會參與
    SVD 遮罩、權重或任何統計計算。
    """

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_workspace_data_path(config_path: Path, logical_path: str) -> Path:
    """在本機與 SERVER 的共同 workspace 結構中解析版本化外部資料。

    `logical_path` 建議使用 `OCM-Data-Preprocessing/...` 這類不含使用者家目錄的路徑。
    搜尋會由設定檔、目前工作目錄與本模組位置逐層往上，讓同一份 JSON 可在 macOS
    `/Users/.../Workspace` 與 SERVER `/home/.../Workspace` 使用。回傳前必須確認是檔案；
    找不到時立即失敗，不能悄悄退回沒有海岸線的圖，否則六區報告會出現版式不一致。
    """

    raw_path = Path(logical_path)
    if raw_path.is_absolute():
        resolved = raw_path.resolve()
        if resolved.is_file():
            return resolved
        raise FileNotFoundError(f"設定指定的外部資料不存在: {resolved}")

    searched: list[Path] = []
    seen: set[Path] = set()
    anchors = (config_path.resolve().parent, Path.cwd().resolve(), Path(__file__).resolve().parent)
    for anchor in anchors:
        for root in (anchor, *anchor.parents):
            candidate = (root / raw_path).resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            searched.append(candidate)
            if candidate.is_file():
                return candidate
    preview = ", ".join(str(path) for path in searched[:8])
    raise FileNotFoundError(f"找不到 figures.coastline_geojson={logical_path}；已搜尋: {preview}")


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


def load_analysis_config(
    config_path: Path,
    *,
    expected_analysis_kind: str = "surface_multivariate_svd",
    expected_variables: tuple[str, str, str] = ("u_surface_mps", "v_surface_mps", "eta_m"),
) -> AnalysisConfig:
    """讀取並驗證三變數 SVD 的共用設定。

    預設仍只接受表層 `u/v/eta`，維持既有 CLI 與重繪器的嚴格契約。固定深度管線會
    顯式傳入自己的 `analysis_kind` 與欄位名稱；兩類分析因而可共用年份、focus bbox、
    缺值門檻、面積權重、圖面與 provenance 驗證，又不必把固定深度偽裝成表層資料。
    `input.year` 是既有單年設定的相容寫法，新的多年度分析仍須使用
    `input.years: [2024, 2025]` 明確列出來源年份。
    """

    raw = _read_json_object(config_path)
    _require(raw.get("schema_version") == CONFIG_SCHEMA_VERSION, f"設定檔必須使用 schema {CONFIG_SCHEMA_VERSION}")
    _require(
        raw.get("analysis_kind") == expected_analysis_kind,
        f"analysis_kind 必須是 {expected_analysis_kind}",
    )

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
    # 嚴格完整月契約以有限時數拒絕過長來源斷點；研究團隊若已核定「全部可得樣本」，
    # 必須明確寫入 null 才會解除上限。欄位缺漏仍視為設定不完整，避免舊設定無意間變成
    # 不限缺口；不論是否設上限，後續都會維持 UTC 軸嚴格遞增並把實際斷點寫入 metadata。
    if "maximum_source_gap_hours" not in input_config:
        raise ValueError("input.maximum_source_gap_hours 必須是有限正數或 null（null 表示接受所有嚴格遞增的來源時間缺口）")
    maximum_source_gap_raw = input_config["maximum_source_gap_hours"]
    if maximum_source_gap_raw is None:
        maximum_source_gap_hours = None
    else:
        maximum_source_gap_hours = _as_float(maximum_source_gap_raw, "input.maximum_source_gap_hours")
        _require(maximum_source_gap_hours > 0.0, "input.maximum_source_gap_hours 若非 null 必須為正數")
    known_time_axis_repairs = _load_known_time_axis_repairs(input_config, years, months, expected_timestep_hours)
    # 預設 reject 保護既有嚴格完整月成果；只有無法重建原始時序且已採「全部可得樣本」
    # 科學契約時，才允許明確選擇 canonicalization。policy 不依賴檔名或 cache_kind 推測，
    # 避免上游將 partial month 改回 ready 後，時間處理規則在沒有版本紀錄的情況下改變。
    time_axis_canonicalization_policy = input_config.get("time_axis_canonicalization_policy", "reject")
    _require(
        time_axis_canonicalization_policy in {"reject", "sort_and_deduplicate_prefer_last"},
        "input.time_axis_canonicalization_policy 必須是 reject 或 sort_and_deduplicate_prefer_last",
    )

    mask_config = raw.get("mask_and_missing_data")
    if not isinstance(mask_config, dict):
        raise ValueError("mask_and_missing_data 必須是物件")
    _require(mask_config.get("static_ocean_mask") == "mask_static.npy", "目前只允許標準 mask_static.npy 作為靜態海域遮罩")

    svd_config = raw.get("svd")
    if not isinstance(svd_config, dict):
        raise ValueError("svd 必須是物件")
    _require(
        svd_config.get("variables") == list(expected_variables),
        f"SVD variables 必須固定為 {', '.join(expected_variables)}",
    )

    parallel_config = raw.get("parallel_execution")
    if not isinstance(parallel_config, dict):
        raise ValueError("parallel_execution 必須是物件")

    figure_config = raw.get("figures")
    if not isinstance(figure_config, dict):
        raise ValueError("figures 必須是物件")
    # 正式流程只允許輸出白底、完整標示的報告圖。舊版無文字透明素材已由專案移除，
    # 因為圖檔一旦脫離 sidecar 就無法判斷變數、單位、模態或研究範圍，容易被誤用。
    # v5 另加入版本化高解析岸線並移除容易被誤認為測站的 anchor 記號。style 只影響
    # figure bundle，不進入重繪器的科學設定雜湊，因此切換不會重新求解 SVD。
    figure_style = figure_config.get("style", "academic_report_ready_v6")
    _require(
        figure_style == "academic_report_ready_v6",
        "figures.style 只允許 academic_report_ready_v6，以確保獨立向量比例尺、明確邊界與版本化岸線",
    )
    figure_formats_raw = figure_config.get("output_formats", ["png"])
    if not isinstance(figure_formats_raw, list) or not figure_formats_raw:
        raise ValueError("figures.output_formats 必須是非空白清單")
    _require(
        all(isinstance(item, str) and item in {"png", "svg"} for item in figure_formats_raw),
        "figures.output_formats 目前只允許 png 與 svg",
    )
    _require(len(figure_formats_raw) == len(set(figure_formats_raw)), "figures.output_formats 不可重複")
    coastline_logical_path = figure_config.get("coastline_geojson")
    coastline_sha256 = figure_config.get("coastline_geojson_sha256")
    if not isinstance(coastline_logical_path, str) or not coastline_logical_path.strip():
        raise ValueError("figures.coastline_geojson 必須是非空白路徑")
    if not isinstance(coastline_sha256, str):
        raise ValueError("figures.coastline_geojson_sha256 必須是小寫 SHA-256")
    _require(
        len(coastline_sha256) == 64
        and all(character in "0123456789abcdef" for character in coastline_sha256),
        "figures.coastline_geojson_sha256 必須是 64 字元小寫十六進位",
    )
    coastline_path = _resolve_workspace_data_path(config_path, coastline_logical_path)
    _require(
        _sha256_file_content(coastline_path) == coastline_sha256,
        "figures.coastline_geojson 內容與設定 SHA-256 不符；必須更新版本與 provenance 後才能產圖",
    )
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
        time_axis_canonicalization_policy=time_axis_canonicalization_policy,
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
        figure_land_overlay_path=coastline_path,
        figure_land_overlay_logical_path=coastline_logical_path,
        figure_land_overlay_sha256=coastline_sha256,
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
    source_time_all = np.concatenate([chunk.time_utc_ns for chunk in chunks], axis=0)
    # 時間 canonicalization 只在設定明確授權時才執行，且以回傳的同一組索引同步重排
    # u/v/eta 與 valid mask，避免只改 time 軸而讓流場樣本錯配。嚴格設定會在此保留原有的
    # 「任何跨月倒序或重複即拒絕」行為；全部可得設定則以可追溯的決定性規則產生唯一 UTC 軸。
    time_all, retained_time_indices, time_axis_canonicalization = _canonicalize_source_time_axis(source_time_all, config)
    fields_all = fields_all[retained_time_indices]
    valid_all = valid_all[retained_time_indices]
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
        time_axis_canonicalization,
    )


def _canonicalize_source_time_axis(
    time_utc_ns: np.ndarray,
    config: AnalysisConfig,
) -> tuple[np.ndarray, np.ndarray, TimeAxisCanonicalization]:
    """依設定建立嚴格遞增、每個 UTC 唯一的分析時間軸與同步資料索引。

    `reject` 供完整月份與其他嚴格科學 run 使用，要求上游串接後本身已嚴格遞增。若原始
    資料在跨日／跨夜邊界有不可考的重複或倒序，`sort_and_deduplicate_prefer_last` 則以
    stable sort 保留同 UTC 最後出現的樣本：輸入順序固定為 config 的年份、月份與月內索引，
    故選擇規則不受 I/O worker 完成順序影響。它不創造、內插或修改流速資料，只捨棄 UTC
    重複觀測中的較早一筆並重排保留樣本；所有影響由回傳摘要保存至成果 metadata。
    """
    """
    處理順序為：
    1. 讀取每個月的 time.npy。
    2. 先在 `_apply_known_time_axis_repairs` 套用「已知」修正；呼叫點在第 820 行。
    3. 合併 2024–2025 全部月份後，呼叫 `_canonicalize_source_time_axis`。
    4. 以 UTC 穩定排序、每個重複 UTC 僅保留來源順序中最後一筆，並同步重排 u/v/eta 等資料；排序與去重的實作在第 946-950 行。
    """

    source_time = np.asarray(time_utc_ns, dtype=np.int64)
    _require(source_time.ndim == 1 and source_time.size >= 2, "跨月份 time_utc_ns 必須是一維且至少兩筆")
    original_indices = np.arange(source_time.size, dtype=np.int64)
    if config.time_axis_canonicalization_policy == "reject":
        _require(np.all(np.diff(source_time) > 0), "跨月份 UTC 時軸有倒序或重複時次")
        return (
            source_time,
            original_indices,
            TimeAxisCanonicalization(
                policy="reject",
                input_time_count=int(source_time.size),
                output_time_count=int(source_time.size),
                reordered_time_step_count=0,
                dropped_duplicate_time_step_count=0,
            ),
        )

    # mergesort 保證相同 UTC 保持輸入月份／月內樣本順序；每段相同 UTC 的最後一筆因而
    # 等價於「後出現樣本優先」。這是原始時刻不可考時唯一不依賴數值內容、可跨區重做的
    # 決定性選擇規則，避免依不同 AOI 的缺值比例挑到不同時間樣本。
    chronological_order = np.argsort(source_time, kind="stable")
    sorted_time = source_time[chronological_order]
    group_ends = np.flatnonzero(np.r_[np.diff(sorted_time) != 0, True])
    retained_indices = chronological_order[group_ends]
    canonical_time = sorted_time[group_ends]
    _require(np.all(np.diff(canonical_time) > 0), "時間 canonicalization 後 UTC 軸仍非嚴格遞增")
    return (
        canonical_time,
        retained_indices,
        TimeAxisCanonicalization(
            policy="sort_and_deduplicate_prefer_last",
            input_time_count=int(source_time.size),
            output_time_count=int(canonical_time.size),
            reordered_time_step_count=int(np.count_nonzero(chronological_order != original_indices)),
            dropped_duplicate_time_step_count=int(source_time.size - canonical_time.size),
        ),
    )


def _validate_source_time_axis(time_utc_ns: np.ndarray, config: AnalysisConfig) -> tuple[float, float, int]:
    """檢查整段資料的逐時節奏與來源斷點，回傳中位步長、最大缺口與斷點數。

    `maximum_source_gap_hours=None` 只解除「缺口長度」的拒絕條件，供已核定的全部可得
    樣本分析使用；它不允許時間倒序、重複時次或非預期的中位採樣頻率。實際最大缺口與
    斷點數仍會保留至成果 metadata，讓報告可如實揭露樣本母體的時間不連續性。
    """

    _require(time_utc_ns.size >= 2, "SVD 至少需要兩個時間樣本")
    diffs_hours = np.diff(time_utc_ns).astype(np.float64) / 3_600_000_000_000.0
    median_hours = float(np.median(diffs_hours))
    maximum_hours = float(np.max(diffs_hours))
    tolerance = max(0.01, config.expected_timestep_hours * 0.01)
    _require(abs(median_hours - config.expected_timestep_hours) <= tolerance, f"時間軸中位步長 {median_hours:.6g} 小時，與設定逐時步長 {config.expected_timestep_hours:.6g} 不一致")
    gap_break_count = int(np.count_nonzero(diffs_hours > config.expected_timestep_hours + tolerance))
    if config.maximum_source_gap_hours is not None:
        _require(maximum_hours <= config.maximum_source_gap_hours, f"最大來源時間缺口 {maximum_hours:.6g} 小時超過設定門檻 {config.maximum_source_gap_hours:.6g} 小時")
    return median_hours, maximum_hours, gap_break_count


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

    source_median_timestep_hours, source_maximum_gap_hours, source_gap_break_count = _validate_source_time_axis(loaded.time_utc_ns, config)
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
        source_median_timestep_hours=source_median_timestep_hours,
        source_maximum_gap_hours=source_maximum_gap_hours,
        source_gap_break_count=source_gap_break_count,
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


def _ring_to_lonlat_array(raw_ring: object) -> np.ndarray | None:
    """把 GeoJSON ring 轉成有限的 WGS84 `(point, lon/lat)` 陣列。

    GeoJSON 可能附帶第三維高程，本表層報告只取前兩欄。少於三個不同頂點、維度錯誤、
    超出合法經緯度或含非有限值的 ring 會被忽略；外部岸線局部壞點不應讓 Matplotlib
    產生外觀正常但投影位置錯誤的陸地。
    """

    ring = np.asarray(raw_ring, dtype=np.float64)
    if ring.ndim != 2 or ring.shape[0] < 4 or ring.shape[1] < 2:
        return None
    lonlat = ring[:, :2]
    if (
        not np.isfinite(lonlat).all()
        or np.any((lonlat[:, 0] < -180.0) | (lonlat[:, 0] > 180.0))
        or np.any((lonlat[:, 1] < -90.0) | (lonlat[:, 1] > 90.0))
    ):
        return None
    if not np.allclose(lonlat[0], lonlat[-1]):
        lonlat = np.vstack((lonlat, lonlat[0]))
    return lonlat


def _iter_geojson_polygon_rings(geometry: dict[str, Any] | None) -> Iterable[tuple[np.ndarray, ...]]:
    """逐一產生 GeoJSON Polygon/MultiPolygon 的外環與洞環。

    每次回傳的 tuple 第一個 ring 是陸地外環，其餘是內部水域洞環。保留洞環可避免
    高解析 OSM land polygons 中的港池、潟湖或其它水域被錯填為陸地；輸入同時接受
    FeatureCollection、Feature 與 GeometryCollection，方便日後更換同格式圖資。
    """

    if not geometry:
        return
    kind = geometry.get("type")
    if kind == "FeatureCollection":
        for feature in geometry.get("features", []):
            if isinstance(feature, dict):
                yield from _iter_geojson_polygon_rings(feature)
        return
    if kind == "Feature":
        nested = geometry.get("geometry")
        if isinstance(nested, dict):
            yield from _iter_geojson_polygon_rings(nested)
        return
    if kind == "GeometryCollection":
        for nested in geometry.get("geometries", []):
            if isinstance(nested, dict):
                yield from _iter_geojson_polygon_rings(nested)
        return
    polygons = (
        geometry.get("coordinates", [])
        if kind == "MultiPolygon"
        else [geometry.get("coordinates", [])]
        if kind == "Polygon"
        else []
    )
    for polygon in polygons:
        converted = tuple(
            ring
            for raw_ring in polygon
            if (ring := _ring_to_lonlat_array(raw_ring)) is not None
        )
        if converted:
            yield converted


def _load_geojson_land_polygons(path: Path) -> tuple[tuple[np.ndarray, ...], ...]:
    """讀取只供圖面使用的版本化陸地多邊形。

    輸出不會 rasterize、覆寫 `valid_mask.npy` 或改變 SVD 狀態向量；它只在所有海洋
    資料圖層上方提供高解析地理參照。若檔案沒有任何可用 polygon，立即停止產圖，
    防止使用者以為已套用岸線，實際卻得到空白底圖。
    """

    payload = _read_json_object(path)
    polygons = tuple(_iter_geojson_polygon_rings(payload))
    _require(bool(polygons), f"岸線 GeoJSON 沒有可用 Polygon/MultiPolygon: {path}")
    return polygons


def _clip_polygon_ring_to_extent(
    ring: np.ndarray,
    extent: tuple[float, float, float, float],
) -> np.ndarray | None:
    """以 Sutherland–Hodgman 將單一 ring 裁到局地經緯度矩形。

    OSM 台灣本島外環含數萬個頂點；若原樣寫入每張 SVG，即使圖面只看 0.15° bbox，
    檔案仍會攜帶整座台灣岸線。矩形裁切保留 bbox 內原始高解析頂點與必要交點，
    同時降低六區多模態 SVG 大小。此函式只改變繪圖幾何，不改動來源 GeoJSON。
    """

    lon_min, lon_max, lat_min, lat_max = extent
    vertices = [np.asarray(point, dtype=np.float64) for point in ring[:-1]]

    def clip_boundary(
        points: list[np.ndarray],
        inside: Any,
        intersect: Any,
    ) -> list[np.ndarray]:
        """將目前 polygon 頂點依單一矩形邊界裁切並插入交點。"""

        if not points:
            return []
        output: list[np.ndarray] = []
        previous = points[-1]
        previous_inside = bool(inside(previous))
        for current in points:
            current_inside = bool(inside(current))
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current))
            previous = current
            previous_inside = current_inside
        return output

    def vertical_intersection(start: np.ndarray, stop: np.ndarray, longitude: float) -> np.ndarray:
        """求線段與固定經度邊界交點；垂直退化線段沿用起點緯度。"""

        delta = stop - start
        fraction = 0.0 if abs(float(delta[0])) < 1.0e-15 else (longitude - float(start[0])) / float(delta[0])
        return np.array([longitude, float(start[1] + fraction * delta[1])], dtype=np.float64)

    def horizontal_intersection(start: np.ndarray, stop: np.ndarray, latitude: float) -> np.ndarray:
        """求線段與固定緯度邊界交點；水平退化線段沿用起點經度。"""

        delta = stop - start
        fraction = 0.0 if abs(float(delta[1])) < 1.0e-15 else (latitude - float(start[1])) / float(delta[1])
        return np.array([float(start[0] + fraction * delta[0]), latitude], dtype=np.float64)

    vertices = clip_boundary(vertices, lambda point: point[0] >= lon_min, lambda a, b: vertical_intersection(a, b, lon_min))
    vertices = clip_boundary(vertices, lambda point: point[0] <= lon_max, lambda a, b: vertical_intersection(a, b, lon_max))
    vertices = clip_boundary(vertices, lambda point: point[1] >= lat_min, lambda a, b: horizontal_intersection(a, b, lat_min))
    vertices = clip_boundary(vertices, lambda point: point[1] <= lat_max, lambda a, b: horizontal_intersection(a, b, lat_max))
    if len(vertices) < 3:
        return None
    clipped = np.asarray(vertices, dtype=np.float64)
    return np.vstack((clipped, clipped[0]))


def _clip_land_polygons_to_extent(
    polygons: tuple[tuple[np.ndarray, ...], ...],
    extent: tuple[float, float, float, float],
) -> tuple[tuple[np.ndarray, ...], ...]:
    """篩選並裁切與正式圖面相交的陸地 polygon，保留可用洞環。"""

    lon_min, lon_max, lat_min, lat_max = extent
    clipped_polygons: list[tuple[np.ndarray, ...]] = []
    for rings in polygons:
        exterior = rings[0]
        overlaps = not (
            float(np.max(exterior[:, 0])) < lon_min
            or float(np.min(exterior[:, 0])) > lon_max
            or float(np.max(exterior[:, 1])) < lat_min
            or float(np.min(exterior[:, 1])) > lat_max
        )
        if not overlaps:
            continue
        clipped_exterior = _clip_polygon_ring_to_extent(exterior, extent)
        if clipped_exterior is None:
            continue
        clipped_holes = tuple(
            clipped
            for hole in rings[1:]
            if (clipped := _clip_polygon_ring_to_extent(hole, extent)) is not None
        )
        clipped_polygons.append((clipped_exterior, *clipped_holes))
    _require(bool(clipped_polygons), "指定岸線 GeoJSON 在 analysis bbox 內沒有任何陸地 polygon")
    return tuple(clipped_polygons)


def _resolve_report_font() -> tuple[str, str]:
    """選擇繁體中文報告字型並回傳字型名稱與檔案 SHA-256。

    字型檔雜湊會進入 figure bundle metadata 的 provenance SHA-256，避免 macOS 與
    SERVER 使用不同字型卻被誤認為相同位元成果。絕對字型路徑不寫入成果，以免洩漏
    主機目錄；若只能 fallback 至 DejaVu Sans，仍保留雜湊供 provenance 稽核。
    """

    import matplotlib.font_manager as font_manager

    available_font_names = {font.name for font in font_manager.fontManager.ttflist}
    candidates = (
        "Noto Sans CJK TC",
        "Noto Sans CJK JP",
        "PingFang TC",
        "Heiti TC",
        "Microsoft JhengHei",
        "WenQuanYi Zen Hei",
        "Arial Unicode MS",
        "DejaVu Sans",
    )
    font_name = next((candidate for candidate in candidates if candidate in available_font_names), "DejaVu Sans")
    font_path = Path(
        font_manager.findfont(
            font_manager.FontProperties(family=font_name),
            fallback_to_default=True,
        )
    )
    return font_name, _sha256_file_content(font_path)


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
    *,
    velocity_context_zh: str = "海表流",
    mean_asset_stem: str = "mean_surface_flow_report",
    mean_asset_key: str = "mean_surface_flow",
) -> list[str]:
    """產生可直接用於簡報與技術報告的白底完整標示圖。

    圖面遵循海洋流場 SVD 文獻常見結構：空間模態以 eta 物理回歸幅度作底色、u/v 回歸
    向量疊圖，並為每個模態另輸出標準化 PC 時序；解釋變異另外輸出 scree/cumulative
    圖。正式 `academic_report_ready_v6` 只在 `figures/report/` 產生白底、完整中文
    解釋變異量、明確經緯度與色階上下限、圖例及版本化高解析海岸線；每張空間圖的
    向量參考尺另存成同 stem 的 `_vector_scale_transparent` 全透明後製素材；另輸出
    `_with_vector_scale` 完整備用圖，把參考尺直接畫在主圖右下角。不提供白底獨立
    比例尺、無文字主圖圖層，也不顯示容易被誤認為測站的 SVD 正負號 anchor。

    `velocity_context_zh` 只改變圖面與指南對速度層位的文字，例如固定深度管線會傳入
    「固定深度 -5 m 流」；`eta` 仍維持唯一自由水面高度。檔名與 metadata key 亦可由
    呼叫端分開指定，避免固定深度成果被誤標為表層平均流。

    PC 遇到來源缺日會斷線，不能用直線跨越沒有資料的時段。PNG 供快速預覽，SVG 保留箭頭
    與曲線向量結構。主圖固定輸出不透明白底；只有檔名明示 `_transparent` 的參考尺
    素材使用 alpha 背景，內容維持單純黑色箭頭與文字，不加入白底或外描邊。
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as MatplotlibPath
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.ticker import FormatStrFormatter
    from matplotlib.transforms import Bbox

    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    report_figure_dir = figure_dir / "report"
    report_figure_dir.mkdir(parents=True, exist_ok=True)
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
    plot_extent = (plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max)
    source_land_polygons = _load_geojson_land_polygons(config.figure_land_overlay_path)
    plot_land_polygons = _clip_land_polygons_to_extent(source_land_polygons, plot_extent)
    coastline_vertex_count = int(
        sum(ring.shape[0] for polygon in plot_land_polygons for ring in polygon)
    )
    # 字型選擇與檔案雜湊由共用 helper 決定；重繪器會把同一簽章納入 bundle metadata
    # 的 provenance SHA-256，確保不同作業系統的字型差異不會被誤認為位元相同成果。
    report_font_name, report_font_sha256 = _resolve_report_font()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [report_font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )

    def save_report_figure(fig: Any, stem: str) -> list[str]:
        """輸出可直接放入簡報／報告的白底 PNG/SVG，並回傳相對路徑。

        圖面必須在深色圖片檢視器、PowerPoint 母片與 PDF 中維持相同白底。小量 padding
        保留座標、色條及最外側文字，避免 `bbox_inches="tight"` 裁掉單位。
        """

        paths: list[str] = []
        fig.patch.set_facecolor("white")
        fig.patch.set_alpha(1.0)
        for output_format in config.figure_formats:
            path = report_figure_dir / f"{stem}.{output_format}"
            save_kwargs: dict[str, Any] = {
                "bbox_inches": "tight",
                "pad_inches": 0.08,
                "transparent": False,
                "facecolor": "white",
            }
            if output_format == "png":
                save_kwargs["dpi"] = config.figure_dpi
            fig.savefig(path, **save_kwargs)
            relative_path = str(path.relative_to(output_dir))
            created.append(relative_path)
            paths.append(relative_path)
        return paths

    def save_vector_scale_asset(
        main_stem: str,
        vector_reference: float,
        vector_unit_label: str,
        reference_arrow_length_inches: float,
    ) -> list[str]:
        """另存完全透明背景的向量參考尺，供 PowerPoint 後製配對使用。

        檔名固定為 `_vector_scale_transparent`，畫布 alpha 為 0，內容只含純黑水平
        箭頭與實際 q95 物理參考量；不畫白色底板、半透明框或 halo。透明 PNG 在深色
        圖片檢視器中可能看起來像黑底，但實際 alpha 仍為透明，PowerPoint 疊圖時不會
        遮住主圖。`reference_arrow_length_inches` 由主圖 q95 quiver 箭頭在 axes 中的
        實際顯示長度換算而來；只要主圖與 SVG 套用相同縮放倍率，獨立參考箭頭就和
        主圖資料箭頭保持同一視覺尺度。此資產只改變版面，不改動向量尺度、SVD 陣列
        或任何科學結果。
        """

        # 畫布先提供足夠空間計算 artist bbox，輸出時再依箭頭與文字的實際 bounding box
        # 四周各保留相同 0.035 inch。這沿用 OCM-NetCDF-Visualizer 與
        # OCM-Data-Preprocessing 的「箭頭緊接東側標籤」結構，但移除固定 30% axes
        # 底框，避免獨立素材出現左右不對稱的大量透明空白。
        fig, axis = plt.subplots(figsize=(1.65, 0.32))
        fig.patch.set_facecolor("none")
        fig.patch.set_alpha(0.0)
        axis.set_facecolor("none")
        axis.patch.set_alpha(0.0)
        axis.set_xlim(0.0, 1.0)
        axis.set_ylim(0.0, 1.0)
        axis.axis("off")
        axis_width_inches = axis.get_position().width * fig.get_figwidth()
        _require(axis_width_inches > 0.0, "透明向量參考尺的 axes 寬度必須為正")
        arrow_start_x = 0.02
        arrow_end_x = arrow_start_x + reference_arrow_length_inches / axis_width_inches
        _require(arrow_end_x < 0.45, "透明向量參考尺的箭頭超出預留畫布")
        arrow_annotation = axis.annotate(
            "",
            xy=(arrow_end_x, 0.50),
            xytext=(arrow_start_x, 0.50),
            arrowprops={
                "arrowstyle": "-|>",
                "color": "black",
                "linewidth": 1.0,
                "mutation_scale": 7.0,
                "shrinkA": 0.0,
                "shrinkB": 0.0,
            },
        )
        text_artist = axis.text(
            arrow_end_x + 0.045,
            0.50,
            f"{vector_reference:.2f} {vector_unit_label}",
            ha="left",
            va="center",
            fontsize=6.6,
            color="black",
        )
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        _require(arrow_annotation.arrow_patch is not None, "透明向量參考尺缺少箭頭 artist")
        content_bbox_inches = Bbox.union(
            [
                arrow_annotation.arrow_patch.get_window_extent(renderer),
                text_artist.get_window_extent(renderer),
            ]
        ).transformed(fig.dpi_scale_trans.inverted())
        symmetric_padding_inches = 0.035
        crop_bbox_inches = Bbox.from_extents(
            content_bbox_inches.x0 - symmetric_padding_inches,
            content_bbox_inches.y0 - symmetric_padding_inches,
            content_bbox_inches.x1 + symmetric_padding_inches,
            content_bbox_inches.y1 + symmetric_padding_inches,
        )
        transparent_paths: list[str] = []
        for output_format in config.figure_formats:
            transparent_path = (
                report_figure_dir / f"{main_stem}_vector_scale_transparent.{output_format}"
            )
            save_kwargs: dict[str, Any] = {
                "bbox_inches": crop_bbox_inches,
                "pad_inches": 0.0,
                "transparent": True,
                "facecolor": "none",
            }
            if output_format == "png":
                save_kwargs["dpi"] = config.figure_dpi
            fig.savefig(transparent_path, **save_kwargs)
            relative_path = str(transparent_path.relative_to(output_dir))
            created.append(relative_path)
            transparent_paths.append(relative_path)
        plt.close(fig)
        return transparent_paths

    def finite_range(field: np.ndarray) -> tuple[float, float]:
        """取得有限值色階；常數場以極小對稱寬度避免 Matplotlib 出現奇異 normalization。"""

        finite = np.asarray(field[np.isfinite(field)], dtype=np.float64)
        _require(finite.size > 0, "學術報告圖的底色欄位至少必須有一個有限值")
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
        sidecar，並用於另存與主圖同 stem 的正式向量比例尺圖。
        """

        magnitude = np.hypot(u_field, v_field)
        finite = np.asarray(magnitude[np.isfinite(magnitude) & (magnitude > 0)], dtype=np.float64)
        _require(finite.size > 0, "流速向量圖至少必須有一個有限非零向量")
        reference = float(np.percentile(finite, 95.0))
        scale = reference / (0.045 * lon_span)
        return reference, scale

    def sampled_quiver_fields(
        u_field: np.ndarray,
        v_field: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """抽取規則箭頭格點並遮蔽邊緣或無效向量。

        距邊界 6% 的向量只在圖面層省略以避免箭頭被裁切；完整 u/v 數值仍保留在
        `.npy`，不會因此改變 SVD 或空間解讀。
        """

        lon_indices = np.arange(0, lon.size, step)
        lat_indices = np.arange(0, lat.size, step)
        quiver_lon = lon_grid[np.ix_(lat_indices, lon_indices)]
        quiver_lat = lat_grid[np.ix_(lat_indices, lon_indices)]
        quiver_u = u_field[np.ix_(lat_indices, lon_indices)]
        quiver_v = v_field[np.ix_(lat_indices, lon_indices)]
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
        return (
            quiver_lon,
            quiver_lat,
            np.ma.masked_where(~interior, quiver_u),
            np.ma.masked_where(~interior, quiver_v),
        )

    def add_land_overlay(axis: Any) -> None:
        """在海洋資料與箭頭上方疊加高解析向量陸地與海岸線。

        陸地採論文海洋圖常見的低彩度暖灰填色，海岸線以深灰細線界定；z-order 高於
        pcolormesh 與 quiver，可遮住模型格點在陸地上的視覺延伸，但不改動任何輸出
        `.npy`。每個 polygon 的洞環與外環組成同一 PathPatch，保留 GeoJSON 水域語意。
        """

        for polygon in plot_land_polygons:
            vertices: list[np.ndarray] = []
            codes: list[np.ndarray] = []
            for ring in polygon:
                ring_codes = np.full(ring.shape[0], MatplotlibPath.LINETO, dtype=np.uint8)
                ring_codes[0] = MatplotlibPath.MOVETO
                ring_codes[-1] = MatplotlibPath.CLOSEPOLY
                vertices.append(ring)
                codes.append(ring_codes)
            path = MatplotlibPath(np.vstack(vertices), np.concatenate(codes))
            axis.add_patch(
                PathPatch(
                    path,
                    facecolor="#D9D6CF",
                    edgecolor="#4A4A4A",
                    linewidth=0.7,
                    joinstyle="round",
                    capstyle="round",
                    zorder=4,
                    clip_on=True,
                )
            )

    def add_report_vector_map(
        stem: str,
        color_field: np.ndarray,
        u_field: np.ndarray,
        v_field: np.ndarray,
        *,
        cmap: str,
        color_limits: tuple[float, float],
        vector_reference: float,
        quiver_scale: float,
        title: str,
        colorbar_label: str,
        vector_unit_label: str,
    ) -> tuple[list[str], list[str], list[str]]:
        """建立標準主圖、透明獨立參考尺，以及內嵌參考尺備用主圖。

        `color_field` 對平均圖是平均 eta（m），對模態圖則是每 1 個標準化 PC 的 eta 回歸
        幅度（m/PC 1σ）；u/v 向量使用相同語意。岸線只作視覺地理參照，來源檔與 SHA-256
        寫入 sidecar；它不會把 OSM polygon 轉成 SVD analysis mask。主圖不疊向量比例尺，
        標準主圖不疊參考尺，方便後製；`_vector_scale_transparent` 是完全透明背景的
        純黑獨立素材。`_with_vector_scale` 則把同一個 q95 參考量直接畫在主圖右下角，
        作為不需 PowerPoint 手動配對的備用完整圖。備用圖的半透明白色小底板只服務
        內嵌標示可讀性，不會出現在獨立透明素材。
        """

        figure_width = 9.0
        map_height = figure_width * (lat_span / lon_span) * geographic_aspect
        fig, axis = plt.subplots(figsize=(figure_width, max(6.3, map_height + 1.25)))
        axis.set_facecolor("white")
        report_cmap = plt.get_cmap(cmap).with_extremes(bad="#E6E6E6")
        # NaN 代表陸地、bbox 外 I/O buffer 或共同有效率未通過格點；用中性淺灰明確區分，
        # 不能讓透明像素在深色背景被誤認成負 loading。
        scalar = axis.pcolormesh(
            lon_grid,
            lat_grid,
            np.ma.masked_invalid(color_field),
            shading="auto",
            cmap=report_cmap,
            vmin=color_limits[0],
            vmax=color_limits[1],
            rasterized=True,
        )
        quiver_lon, quiver_lat, quiver_u, quiver_v = sampled_quiver_fields(u_field, v_field)
        quiver_artist = axis.quiver(
            quiver_lon,
            quiver_lat,
            quiver_u,
            quiver_v,
            color="black",
            angles="xy",
            scale_units="xy",
            scale=quiver_scale,
            pivot="mid",
            width=0.0032,
            headwidth=3.2,
            headlength=4.2,
            headaxislength=3.8,
            zorder=3,
        )
        # 岸線在箭頭之上，避免近岸格點的箭頭跨到陸地；黃色 anchor 不再顯示，因其只是
        # SVD 正負號數值慣例而非測站，正式圖保留會造成觀測位置誤讀。
        add_land_overlay(axis)
        axis.set_xlim(plot_lon_min, plot_lon_max)
        axis.set_ylim(plot_lat_min, plot_lat_max)
        axis.set_aspect(geographic_aspect, adjustable="box")
        axis.set_title(title, fontsize=16, pad=18)
        axis.set_xlabel("經度（°E）", fontsize=12)
        axis.set_ylabel("緯度（°N）", fontsize=12)
        # 以含頭含尾的等距刻度明確顯示圖面實際經緯度上下限；相較自動 locator，
        # 不會因不同 bbox 或 Matplotlib 版本而省略邊界值。三位小數可辨識 OCM 1 km
        # focus bbox 的邊界，同時避免標籤因過多有效位數互相重疊。
        longitude_ticks = np.linspace(plot_lon_min, plot_lon_max, 6)
        latitude_ticks = np.linspace(plot_lat_min, plot_lat_max, 6)
        axis.set_xticks(longitude_ticks)
        axis.set_yticks(latitude_ticks)
        axis.xaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.yaxis.set_major_formatter(FormatStrFormatter("%.03f"))
        axis.tick_params(labelsize=10)
        axis.grid(color="white", linewidth=0.45, alpha=0.35)
        colorbar = fig.colorbar(scalar, ax=axis, pad=0.025, fraction=0.047)
        # 色條固定列出包含 vmin、vmax 的五個刻度，確保讀者不必由色塊或自動刻度推測
        # 上下限。`%.3g` 在一般小數與極小科學記號間自動切換，可跨六區保留有效位數。
        colorbar_ticks = np.linspace(color_limits[0], color_limits[1], 5)
        colorbar.set_ticks(colorbar_ticks)
        colorbar.ax.yaxis.set_major_formatter(FormatStrFormatter("%.3g"))
        colorbar.update_ticks()
        colorbar.set_label(colorbar_label, fontsize=11)
        colorbar.ax.tick_params(labelsize=9)
        fig.tight_layout()
        map_paths = save_report_figure(fig, stem)

        # OCM-NetCDF-Visualizer 與 OCM-Data-Preprocessing 都把 quiverkey 固定在 axes
        # 右下角、使用 labelpos="E"，並以半透明矩形維持跨色階可讀性。本圖沿用相同
        # 結構，但因 SVD 單位字串較長，使用 26.0%×6.0% 的緊湊矩形與一般字重，
        # 比上一版右上角 31.5% 寬圓角框更小、更對稱，也不會與標題或色條互相擠壓。
        key_panel = Rectangle(
            (0.715, 0.022),
            0.260,
            0.060,
            transform=axis.transAxes,
            facecolor="#f8fbfc",
            edgecolor="#4A4A4A",
            linewidth=0.45,
            alpha=0.86,
            zorder=5,
            clip_on=False,
        )
        axis.add_patch(key_panel)
        axis.quiverkey(
            quiver_artist,
            X=0.810,
            Y=0.050,
            U=vector_reference,
            label=f"{vector_reference:.2f} {vector_unit_label}",
            labelpos="E",
            labelsep=0.045,
            coordinates="axes",
            color="black",
            fontproperties={"size": 7.2},
            zorder=6,
        )
        embedded_scale_map_paths = save_report_figure(fig, f"{stem}_with_vector_scale")
        # 主圖 quiver 對 q95 參考量的資料長度固定為 bbox 經度寬度的 4.5%；轉成 inches
        # 傳給獨立素材，使它與主圖按相同比例縮放時仍能逐箭頭比較。
        reference_arrow_length_inches = 0.045 * axis.bbox.width / fig.dpi
        plt.close(fig)
        transparent_scale_paths = save_vector_scale_asset(
            stem,
            vector_reference,
            vector_unit_label,
            reference_arrow_length_inches,
        )
        return map_paths, transparent_scale_paths, embedded_scale_map_paths

    mean_color_limits = finite_range(mean_eta)
    mean_vector_reference, mean_quiver_scale = vector_scale(mean_u, mean_v)
    (
        mean_report_paths,
        mean_vector_scale_transparent_paths,
        mean_with_vector_scale_paths,
    ) = add_report_vector_map(
        mean_asset_stem,
        mean_eta,
        mean_u,
        mean_v,
        cmap="viridis",
        color_limits=mean_color_limits,
        vector_reference=mean_vector_reference,
        quiver_scale=mean_quiver_scale,
        title=f"{config.focus_name_zh}：{_configured_year_label(config)} 全部可得樣本平均{velocity_context_zh}與海面高度",
        colorbar_label="平均海面高度 η（m）",
        # 比例尺獨立圖使用純文字 `m/s` 而不啟動 Matplotlib mathtext parser；後者在
        # 六區平行重繪時可能競爭共用 parser cache，且部分本機 CJK 字型缺少上標負號。
        vector_unit_label="m/s",
    )
    asset_metadata[mean_asset_key] = {
        "report_files": mean_report_paths,
        "vector_scale_transparent_report_files": mean_vector_scale_transparent_paths,
        "with_vector_scale_report_files": mean_with_vector_scale_paths,
        "eta_color_map": "viridis",
        "eta_color_limits_m": list(mean_color_limits),
        "eta_colorbar_ticks_m": np.linspace(mean_color_limits[0], mean_color_limits[1], 5).tolist(),
        "vector_reference_mps_at_95th_percentile": mean_vector_reference,
        "matplotlib_quiver_scale": mean_quiver_scale,
    }

    time_datetime = time_utc_ns.astype("datetime64[ns]")
    report_time_start = time_datetime[0].astype("datetime64[M]")
    # 右界取最後一個月的最後 1 ns，而不是下一月月初；如此全年 PC 圖會顯示 01–12，
    # 不會在最右端再出現一個容易被誤認為同年一月的「01」刻度。
    report_time_stop = (
        time_datetime[-1].astype("datetime64[M]")
        + np.timedelta64(1, "M")
        - np.timedelta64(1, "ns")
    )
    report_time_formatter = "%m" if len(config.years) == 1 else "%Y-%m"
    diffs_hours = np.diff(time_utc_ns).astype(np.float64) / 3_600_000_000_000.0
    gap_after_indices = np.where(diffs_hours > config.expected_timestep_hours * 1.5)[0]
    segment_starts = np.concatenate((np.array([0], dtype=int), gap_after_indices + 1))
    segment_stops = np.concatenate((gap_after_indices + 1, np.array([time_utc_ns.size], dtype=int)))
    time_days = time_datetime.astype("datetime64[D]")
    unique_days, day_inverse = np.unique(time_days, return_inverse=True)
    daily_gap_after_indices = np.where(np.diff(unique_days).astype("timedelta64[D]").astype(np.int64) > 1)[0]
    daily_segment_starts = np.concatenate((np.array([0], dtype=int), daily_gap_after_indices + 1))
    daily_segment_stops = np.concatenate((daily_gap_after_indices + 1, np.array([unique_days.size], dtype=int)))
    # SERVER 目前使用 Python 3.9，不能使用 Python 3.10 才加入的
    # `zip(..., strict=True)`。先顯式驗證每段的起訖索引數量一致，再以一般 zip
    # 配對，既保留 strict 模式原本要防止的靜默截斷檢查，也維持本機與 SERVER
    # 共用同一套繪圖程式。這些索引只控制 PC 折線在缺測時間處斷開，不會改動樣本值。
    _require(
        segment_starts.size == segment_stops.size,
        "逐時 PC 缺測分段的起點與終點數量不一致",
    )
    _require(
        daily_segment_starts.size == daily_segment_stops.size,
        "逐日 PC 缺測分段的起點與終點數量不一致",
    )
    figure_mode_count = min(config.figure_mode_count, visualization.regression_u.shape[0])
    for mode_index in range(figure_mode_count):
        mode_number = mode_index + 1
        eta_limit = symmetric_limit(visualization.regression_eta[mode_index])
        vector_reference, quiver_scale = vector_scale(
            visualization.regression_u[mode_index],
            visualization.regression_v[mode_index],
        )

        # 淡灰逐時線保留原始高頻結構，黑色日平均線提供全年尺度可讀輪廓；這對應海洋流場
        # SVD 論文常見的 raw + smoothed time mode 疊圖。兩層都分段繪製，缺日不會被連線。
        pc_values = visualization.pc_standardized[mode_index]
        daily_values = np.array(
            [float(np.mean(pc_values[day_inverse == day_index])) for day_index in range(unique_days.size)],
            dtype=np.float64,
        )
        pc_limit = max(float(np.max(np.abs(pc_values))), 1.0)
        explained_percent = float(explained_variance[mode_index] * 100.0)
        # 表層既有成果的標題格式已經是報告基準，因此「海表流」不額外加括號；
        # 固定深度重繪則必須把速度場所代表的物理深度寫進主圖，避免不同深度使用
        # 同一份海面高度 η 時，被誤讀成四張完全相同的表層分析。這個字串只改圖面
        # 說明，不會改動回歸場、向量比例尺或任何 SVD 數值。
        velocity_title_context = (
            "" if velocity_context_zh == "海表流" else f"（{velocity_context_zh}）"
        )
        (
            report_spatial_paths,
            report_vector_scale_transparent_paths,
            report_with_vector_scale_paths,
        ) = add_report_vector_map(
            f"svd_mode_{mode_number:02d}_spatial_report",
            visualization.regression_eta[mode_index],
            visualization.regression_u[mode_index],
            visualization.regression_v[mode_index],
            cmap="RdBu_r",
            color_limits=(-eta_limit, eta_limit),
            vector_reference=vector_reference,
            quiver_scale=quiver_scale,
            title=(
                f"{config.focus_name_zh}{velocity_title_context}：SVD 模態 {mode_number}"
                f"（解釋變異量：{explained_percent:.2f}%）"
            ),
            colorbar_label="η 回歸幅度（m / PC 1σ）",
            vector_unit_label="m/s / PC 1σ",
        )

        # 報告用 PC 圖保留逐時與逐日兩個時間尺度，並讓缺測斷點維持空白。月份刻度
        # 固定由 UTC time axis 產生，避免本機時區將月底樣本移到相鄰月份。
        fig, axis = plt.subplots(figsize=(11.2, 4.0))
        fig.patch.set_facecolor("white")
        axis.set_facecolor("white")
        axis.axhline(0.0, color="#666666", linewidth=0.75, alpha=0.9)
        for start, stop in zip(segment_starts, segment_stops):
            axis.plot(
                time_datetime[start:stop],
                pc_values[start:stop],
                color="#8A8A8A",
                linewidth=0.38,
                alpha=0.28,
            )
        for start, stop in zip(daily_segment_starts, daily_segment_stops):
            axis.plot(
                unique_days[start:stop],
                daily_values[start:stop],
                color="black",
                linewidth=1.15,
            )
        axis.set_xlim(report_time_start, report_time_stop)
        axis.set_ylim(-pc_limit * 1.04, pc_limit * 1.04)
        axis.set_title(
            (
                f"{config.focus_name_zh}：模態 {mode_number} 標準化 PC"
                f"（解釋變異量：{explained_percent:.2f}%）"
            ),
            fontsize=15,
            pad=12,
        )
        axis.set_xlabel(f"{_configured_year_label(config)} 年月份（UTC）", fontsize=11)
        axis.set_ylabel("標準化 PC（σ）", fontsize=11)
        axis.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        axis.xaxis.set_major_formatter(mdates.DateFormatter(report_time_formatter))
        axis.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
        # 使用舊版與新版 Matplotlib 都支援的 `ncol`；SERVER 的既有科學環境
        # 尚未接受較新的 `ncols` 別名。這只控制三個圖例項目橫向排列，不改變
        # PC 值、時間軸或任何 SVD 計算結果。
        axis.legend(
            handles=[
                Line2D([0], [0], color="#8A8A8A", linewidth=1.0, alpha=0.55, label="逐時 PC"),
                Line2D([0], [0], color="black", linewidth=1.4, label="逐日平均"),
                Line2D([0], [0], color="white", linewidth=0, label="缺測處斷線"),
            ],
            loc="upper right",
            ncol=3,
            frameon=True,
            facecolor="white",
            framealpha=0.92,
        )
        fig.tight_layout()
        report_pc_paths = save_report_figure(fig, f"svd_mode_{mode_number:02d}_pc_report")
        plt.close(fig)
        asset_metadata["modes"].append(
            {
                "mode": mode_number,
                "explained_variance_fraction": float(explained_variance[mode_index]),
                "report_spatial_files": report_spatial_paths,
                "vector_scale_transparent_report_files": report_vector_scale_transparent_paths,
                "with_vector_scale_report_files": report_with_vector_scale_paths,
                "report_pc_files": report_pc_paths,
                "eta_color_map": "RdBu_r",
                "eta_symmetric_color_limit_m_per_pc_standard_deviation": eta_limit,
                "eta_colorbar_ticks_m_per_pc_standard_deviation": np.linspace(
                    -eta_limit,
                    eta_limit,
                    5,
                ).tolist(),
                "vector_reference_mps_per_pc_standard_deviation_at_95th_percentile": vector_reference,
                "matplotlib_quiver_scale": quiver_scale,
                "pc_y_symmetric_limit_standard_deviation": pc_limit * 1.04,
                "pc_display_layers": ["hourly standardized PC in translucent gray", "daily mean standardized PC in black"],
            }
        )

    mode_numbers = np.arange(1, explained_variance.size + 1)
    cumulative_percent = np.cumsum(explained_variance) * 100.0
    individual_percent = explained_variance * 100.0
    fig, axis = plt.subplots(figsize=(10.0, 5.3))
    fig.patch.set_facecolor("white")
    axis.set_facecolor("white")
    bars = axis.bar(
        mode_numbers,
        individual_percent,
        width=0.72,
        color="#2A6F97",
        label="單一模態解釋變異量",
        zorder=2,
    )
    axis.plot(
        mode_numbers,
        cumulative_percent,
        color="#202020",
        linewidth=1.5,
        marker="o",
        markersize=4.2,
        label="累積解釋變異量",
        zorder=3,
    )
    # 前五模態是正式圖面交付範圍；直接標出完整的解釋變異百分比，可讓簡報讀者
    # 不必從 y 軸估算，也避免只寫 EV 縮寫而被誤解為其他統計量。
    for index, bar in enumerate(bars[:figure_mode_count]):
        axis.text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height() + 1.2,
            f"{individual_percent[index]:.2f}%",
            ha="center",
            va="bottom",
            fontsize=8.5,
        )
    for index in sorted({min(1, explained_variance.size - 1), min(4, explained_variance.size - 1)}):
        axis.annotate(
            f"累積 {cumulative_percent[index]:.2f}%",
            xy=(mode_numbers[index], cumulative_percent[index]),
            xytext=(7, -18 if index == 1 else 10),
            textcoords="offset points",
            fontsize=9,
            color="#202020",
        )
    axis.set_title(f"{config.focus_name_zh}：SVD 模態解釋變異", fontsize=15, pad=12)
    axis.set_xlabel("模態編號", fontsize=11)
    axis.set_ylabel("解釋變異（%）", fontsize=11)
    axis.set_xlim(0.35, float(mode_numbers[-1]) + 0.65)
    axis.set_ylim(0.0, 105.0)
    axis.set_xticks(mode_numbers)
    axis.grid(axis="y", color="#D9D9D9", linewidth=0.65, alpha=0.9, zorder=1)
    axis.legend(loc="center right", frameon=True, facecolor="white", framealpha=0.92)
    fig.tight_layout()
    explained_report_paths = save_report_figure(fig, "svd_explained_variance_report")
    plt.close(fig)
    asset_metadata["explained_variance"] = {
        "report_files": explained_report_paths,
        "individual_fraction": explained_variance.tolist(),
        "cumulative_fraction": np.cumsum(explained_variance).tolist(),
    }

    # 每個 immutable bundle 自帶最小報告指南，避免圖檔脫離專案 README 後又失去
    # 「顏色／箭頭／PC 如何一起讀」的必要語意。理論樣本數依設定年份與月份計算；
    # 這是時數覆蓋率，不主張能代表原始來源品質，正式限制仍以 run metadata 為準。
    import calendar

    expected_time_count = sum(
        calendar.monthrange(year, month)[1] * int(round(24.0 / config.expected_timestep_hours))
        for year in config.years
        for month in config.months
    )
    retained_time_count = int(time_utc_ns.size)
    time_coverage_fraction = retained_time_count / expected_time_count if expected_time_count else float("nan")
    maximum_gap_hours = float(np.max(diffs_hours)) if diffs_hours.size else 0.0
    cumulative = np.cumsum(explained_variance)
    mode_rows = "\n".join(
        (
            f"| {mode_index + 1} | {explained_variance[mode_index] * 100.0:.2f}% | "
            f"{cumulative[mode_index] * 100.0:.2f}% | "
            f"{asset_metadata['modes'][mode_index]['eta_symmetric_color_limit_m_per_pc_standard_deviation']:.3f} m | "
            f"{asset_metadata['modes'][mode_index]['vector_reference_mps_per_pc_standard_deviation_at_95th_percentile']:.3f} m s⁻¹ |"
        )
        for mode_index in range(figure_mode_count)
    )
    report_guide = f"""# SVD 圖表報告指南

本 bundle 的 `report/` 主圖是可直接放入簡報或技術報告的不透明白底完整標示版；
只有檔名明示 `_vector_scale_transparent` 的向量參考尺是透明後製素材。

## 分析範圍與資料覆蓋

- 區域：{config.focus_name_zh}
- 速度層位：{velocity_context_zh}；η 始終是同時次、同水平格點的自由水面高度，不是該深度的另一個 η。
- analysis bbox（lon_min, lon_max, lat_min, lat_max）：{list(config.bbox)}
- 可得樣本：{retained_time_count:,} / {expected_time_count:,} 小時（{time_coverage_fraction * 100.0:.2f}%）
- 圖面時間範圍：{_iso_utc_from_ns(int(time_utc_ns[0]))} 至 {_iso_utc_from_ns(int(time_utc_ns[-1]))}
- 來源時間斷點：{int(gap_after_indices.size)} 個；最大相鄰樣本間隔 {maximum_gap_hours:.1f} 小時

本成果使用設定期間內全部可得樣本。缺測處在 PC 圖中斷線，不跨缺口插值；報告中仍應
揭露實際覆蓋率與缺口，不能把「年度可得資料」寫成無缺測的完整逐時觀測。

## 地理底圖

- 陸地來源：OSMData land-polygons（OpenStreetMap `natural=coastline` 衍生，ODbL）。
- 版本化路徑：`{config.figure_land_overlay_logical_path}`
- SHA-256：`{config.figure_land_overlay_sha256}`
- 圖中暖灰色為向量陸地、深灰線為海岸線；只提供地理參照，不改變 OCM 1 km 流場、
  SVD 海域遮罩、權重或統計結果。
- 圖上不顯示 SVD 正負號參考點，避免被誤認為測站或觀測位置。

## 前五模態

| 模態 | 單一模態解釋變異量 | 累積解釋變異量 | η 對稱色階上限（每 PC 1σ） | 箭頭 q95 參考量（每 PC 1σ） |
|---:|---:|---:|---:|---:|
{mode_rows}

## 讀圖規則

1. 空間圖底色是 η 回歸幅度；紅為正、藍為負，單位是 m / PC 1σ。
2. 黑箭頭是 u/v 回歸向量，單位是 m s⁻¹ / PC 1σ；不是任一時刻的實際流速。
   標準空間圖不預先疊入向量參考尺。PowerPoint 後製使用同 stem 的
   `_vector_scale_transparent` SVG；它是全透明背景、純黑箭頭與數值，已依內容緊密
   對稱裁切，且參考箭頭長度對齊主圖 q95 箭頭。兩張圖以原始尺寸匯入後應先群組，再
   一起縮放；放在右下角並內縮約 2.5%。若不想手動後製，直接使用同 stem 的
   `_with_vector_scale` 備用完整圖；該圖已在右下角放入同尺度參考箭頭。
3. 某時刻單一模態的距平貢獻 = 空間回歸圖樣 × 同模態標準化 PC。
4. PC 為正時照圖例方向／色號解讀；PC 為負時箭頭與 η 正負全部反轉。
5. SVD 模態整體乘以 -1 仍是同一解，因此不可脫離 PC 單獨把藍色命名為「下降事件」。
6. 解釋變異量表示此模態解釋三變數正規化、面積加權總變異的比例，不是流速或海面高度的百分比。

## 報告建議

- 先用 `svd_explained_variance_report` 說明模態保留依據。
- 每個模態把 `svd_mode_XX_spatial_report` 與 `svd_mode_XX_pc_report` 成對呈現；
  需要手動疊回主圖時，使用同 stem 的
  `svd_mode_XX_spatial_report_vector_scale_transparent`；若不後製，改用
  `svd_mode_XX_spatial_report_with_vector_scale` 備用完整圖。
- 用空間圖說明「共同變動形態」，再用 PC 說明該形態在全年何時偏正或偏負。
- 平均流圖是全年平均背景場；SVD 模態是距平變動，兩者不可相加敘述為同一張瞬時流場。
"""
    report_guide_path = figure_dir / "REPORT_GUIDE.md"
    report_guide_path.write_text(report_guide, encoding="utf-8")
    created.append(str(report_guide_path.relative_to(output_dir)))

    plot_metadata = {
        "schema_name": "ocm_svd_academic_report_ready_figure_assets",
        # 6.3.0 依相鄰 OCM 專案把內嵌 quiverkey 移至右下角並縮成緊湊矩形；透明
        # 素材改依 artist bbox 對稱裁切，且箭頭沿用主圖 q95 的實際顯示長度。
        "schema_version": "6.3.0",
        "style": config.figure_style,
        "text_policy": {
            "assets_contain_text": True,
            "assets_purpose": "提供白底完整報告主圖、全透明純黑獨立向量參考尺，以及已內嵌同尺度參考尺的備用完整圖。",
        },
        "rendering": {
            "formats": list(config.figure_formats),
            "png_dpi": config.figure_dpi,
            "report_background": "opaque white",
            "report_font_name": report_font_name,
            "report_font_file_sha256": report_font_sha256,
            "analysis_bbox_lon_lat": list(config.bbox),
            "plotted_valid_cell_edge_bbox_lon_lat": [plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max],
            "longitude_axis_ticks_degrees_east": np.linspace(plot_lon_min, plot_lon_max, 6).tolist(),
            "latitude_axis_ticks_degrees_north": np.linspace(plot_lat_min, plot_lat_max, 6).tolist(),
            "axis_boundary_policy": "first and last visible ticks equal the plotted valid-cell-edge bbox limits",
            "colorbar_boundary_policy": "first and last visible ticks equal the scalar vmin and vmax",
            "vector_scale_policy": "standard map has no key; save transparent <main_stem>_vector_scale_transparent plus complete <main_stem>_with_vector_scale backup",
            "vector_scale_layout": {
                "transparent_asset_background": "fully transparent alpha",
                "transparent_asset_content": "plain black arrow and text; no white background, panel, or halo",
                "transparent_asset_crop": "tight artist bounding box with symmetric 0.035 inch padding",
                "reference_arrow_display_length_fraction_of_axes_width": 0.045,
                "embedded_backup_panel": "compact 26.0% by 6.0% light panel at alpha 0.86 inside lower-right axes",
                "layout_reference_projects": [
                    "OCM-NetCDF-Visualizer/scripts/visualize_ocm_month.py",
                    "OCM-Data-Preprocessing/scripts/visualize_ocm_surface_cache.py",
                ],
                "recommended_postproduction_scale": "import main and transparent SVG at intrinsic size, then group and resize together",
                "recommended_anchor": "inside lower right with approximately 2.5% inset",
            },
            "quiver_grid_stride": step,
        },
        "geographic_context": {
            "land_overlay_source": "OSMData land-polygons derived from OpenStreetMap natural=coastline",
            "license": "OpenStreetMap data / ODbL; attribution required",
            "logical_path": config.figure_land_overlay_logical_path,
            "sha256": config.figure_land_overlay_sha256,
            "format_and_crs": "GeoJSON Polygon/MultiPolygon; WGS84 longitude/latitude",
            "source_polygon_count": len(source_land_polygons),
            "plotted_polygon_count": len(plot_land_polygons),
            "plotted_vertex_count_after_bbox_clipping": coastline_vertex_count,
            "land_fill": "#D9D6CF",
            "coastline_stroke": "#4A4A4A",
            "semantics": "visual geographic reference only; does not alter SVD masks, arrays, weights, or statistics",
        },
        "spatial_pattern_representation": {
            "pc": "每一模態以樣本標準差 ddof=1 標準化為無因次 PC。",
            "scalar_background": "eta 回歸空間模態，單位 m per 1 standard deviation of PC。",
            "vectors": f"{velocity_context_zh} u/v 回歸空間模態，單位 m s-1 per 1 standard deviation of PC。",
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
                "applied_convention": f"{velocity_context_zh} SVD 空間向量圖與對應時間模態分開呈現，向量抽稀以維持可讀性。",
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
    """以科學設定與月份 metadata hash 建立穩定 run ID。

    `figures` 刻意排除：岸線、DPI 或字型只改變獨立 figure bundle，不應讓完全相同的
    SVD 陣列取得另一個 science run ID。這也讓 2024 完備後能先完成六區科學 run，再用
    同一組陣列反覆改善報告版式，而不重算或複製大型結果。
    """

    source_signature = [{"month": item.month_id, "metadata_sha256": item.metadata_sha256} for item in source_months]
    science_config = {key: value for key, value in config.raw.items() if key != "figures"}
    digest = _canonical_json_hash({"config": science_config, "source_months": source_signature})[:12]
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
                "time_axis_canonicalization": {
                    "policy": loaded.time_axis_canonicalization.policy,
                    "input_time_count": loaded.time_axis_canonicalization.input_time_count,
                    "output_time_count": loaded.time_axis_canonicalization.output_time_count,
                    "reordered_time_step_count": loaded.time_axis_canonicalization.reordered_time_step_count,
                    "dropped_duplicate_time_step_count": loaded.time_axis_canonicalization.dropped_duplicate_time_step_count,
                    "semantics": "sort_and_deduplicate_prefer_last 先以 UTC 穩定排序，再保留每個重複 UTC 在設定年份、月份與月內索引序列中最後出現的樣本；它不補值、不改 u/v/eta 數值，且只在設定明確授權時使用。",
                },
                "source_time_axis": {
                    "expected_timestep_hours": config.expected_timestep_hours,
                    "median_timestep_hours": prepared.source_median_timestep_hours,
                    "maximum_gap_hours": prepared.source_maximum_gap_hours,
                    "gap_break_count": prepared.source_gap_break_count,
                    "maximum_gap_limit_hours": config.maximum_source_gap_hours,
                    "maximum_gap_policy": "unbounded_but_reported" if config.maximum_source_gap_hours is None else "bounded_and_validated",
                    "semantics": "gap_break_count 計數相鄰 UTC 間隔大於預期步長加容差的來源斷點；解除上限時仍要求 UTC 軸嚴格遞增及中位步長符合設定。",
                },
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
