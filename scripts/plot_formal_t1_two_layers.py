#!/usr/bin/env python3
"""繪製正式兩年 SVD 衍生的 t1 兩層格網展示圖。

本腳本只讀取 ``work/formal_t1_two_layers`` 已產生的五個物理場 ``.npy``，以及
``feature_index_map.csv`` 中的正式格點經緯度。它不重新建立兩年矩陣、不重新執行
SVD，也不修改正式水柱 SVD run；用途是把已回填為 ``(102, 152)`` 的 t1 rank-20
物理值直接轉成 PNG 圖面。

輸入物理場的定義是正式前 20 個模態在第一個保留 UTC 時次的重建：

    q_rank20(p, t1) = mean(p) + sum_k(mode_k(p) * PC_k(t1))

``NaN`` 代表正式 mask=False 的無效格點，繪圖時會保持遮罩，不會誤填成零。除五張
單一物理場圖外，腳本也會產生表層與 10 m 的 u/v 向量方向圖；向量箭頭供空間方向
辨識，箭頭長度是視覺化比例，不是地理距離與速度單位的一對一比例尺。
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

# SERVER 或 CI 通常沒有圖形桌面；Agg 可在無 GUI 環境穩定輸出 PNG。
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm


DEFAULT_INPUT_DIR = Path("work/formal_t1_two_layers")
GRID_SHAPE = (102, 152)
QUIVER_STRIDE = 8


# 每個項目對應一個已回填的物理圖場。signed=True 表示速度可正可負，色階以零為中心。
FIELD_SPECS = (
    {
        "name": "eta",
        "filename": "roundtrip_eta_physical_t1_rank20.npy",
        "title": "η：正式 rank-20 t1 重建",
        "colorbar": "η (m)",
        "cmap": "viridis",
        "signed": False,
    },
    {
        "name": "u_surface",
        "filename": "roundtrip_u_surface_physical_t1_rank20.npy",
        "title": "u 表層：正式 rank-20 t1 重建",
        "colorbar": "u (m/s)",
        "cmap": "RdBu_r",
        "signed": True,
    },
    {
        "name": "u_10m",
        "filename": "roundtrip_u_10m_physical_t1_rank20.npy",
        "title": "u 10 m：正式 rank-20 t1 重建",
        "colorbar": "u (m/s)",
        "cmap": "RdBu_r",
        "signed": True,
    },
    {
        "name": "v_surface",
        "filename": "roundtrip_v_surface_physical_t1_rank20.npy",
        "title": "v 表層：正式 rank-20 t1 重建",
        "colorbar": "v (m/s)",
        "cmap": "RdBu_r",
        "signed": True,
    },
    {
        "name": "v_10m",
        "filename": "roundtrip_v_10m_physical_t1_rank20.npy",
        "title": "v 10 m：正式 rank-20 t1 重建",
        "colorbar": "v (m/s)",
        "cmap": "RdBu_r",
        "signed": True,
    },
)


def configure_report_font() -> str:
    """選擇可顯示繁體中文的字型，避免圖面標題出現缺字方框。

    本機通常有 Noto Sans TC、Heiti TC 或 Songti TC；SERVER 若安裝 Noto Sans CJK，
    也會優先使用。若執行環境沒有任何 CJK 字型，才退回 DejaVu Sans，這時中文仍可能
    顯示為缺字警告，但數值圖場與輸出流程不會被改變。
    """

    import matplotlib.font_manager as font_manager

    candidates = (
        "Noto Sans CJK TC",
        "Heiti TC",
        "PingFang TC",
        "Noto Sans TC",
        "Songti TC",
        "Arial Unicode MS",
        "DejaVu Sans",
    )
    available = {font.name for font in font_manager.fontManager.ttflist}
    font_name = next((candidate for candidate in candidates if candidate in available), "DejaVu Sans")
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    return font_name


def read_json(path: Path) -> dict:
    """讀取展示 metadata，確保腳本知道圖面對應的正式來源與 t1 時次。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"metadata 必須是 JSON object：{path}")
    return value


def format_t1_utc(metadata: dict) -> str:
    """把 metadata 保存的 epoch nanoseconds 轉成可讀 UTC 標籤。

    t1 是展示欄唯一的時間欄；用整秒轉換即可，避免在圖面標籤中顯示難以閱讀的
    nanoseconds 整數，同時保留 UTC 時區避免誤解成台灣時間。
    """

    epoch_ns = int(metadata["source_t1_utc_ns"])
    timestamp = datetime.fromtimestamp(epoch_ns // 1_000_000_000, tz=timezone.utc)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def load_grid_axes(index_map_path: Path, grid_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """由正式 row/col 對照表重建 1D longitude、latitude 軸。

    ``roundtrip_*`` 陣列本身只保存數值，不重複保存座標；CSV 是座標與 feature row 的
    審計來源。腳本會檢查每個 row/col 都有座標，且同一格點的重複座標一致，避免把
    feature 順序誤當成繪圖座標。
    """

    nlat, nlon = grid_shape
    lat = np.full(nlat, np.nan, dtype=np.float64)
    lon = np.full(nlon, np.nan, dtype=np.float64)

    with index_map_path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            row = int(record["grid_row_zero_based"])
            col = int(record["grid_col_zero_based"])
            if not (0 <= row < nlat and 0 <= col < nlon):
                raise ValueError(f"CSV 格點超出 {grid_shape} 範圍：row={row}, col={col}")

            record_lat = float(record["latitude"])
            record_lon = float(record["longitude"])
            if np.isfinite(lat[row]) and not np.isclose(lat[row], record_lat):
                raise ValueError(f"同一 row 的 latitude 不一致：row={row}")
            if np.isfinite(lon[col]) and not np.isclose(lon[col], record_lon):
                raise ValueError(f"同一 col 的 longitude 不一致：col={col}")
            lat[row] = record_lat
            lon[col] = record_lon

    if not np.all(np.isfinite(lat)) or not np.all(np.isfinite(lon)):
        raise ValueError("feature_index_map.csv 沒有提供完整 102×152 座標軸")
    if not np.all(np.diff(lat) > 0) or not np.all(np.diff(lon) > 0):
        raise ValueError("正式格網座標必須沿 row/col 遞增，才能直接使用 pcolormesh")
    return lon, lat


def load_field(input_dir: Path, filename: str) -> np.ndarray:
    """載入一個物理圖場並檢查其確實是完整正式格網。

    陣列必須維持 ``[grid_row, grid_col]``，也就是 ``[latitude_index, longitude_index]``；
    不在此處轉置，避免把主持人要求的 row/col 排列與正式回填結果弄反。
    """

    path = input_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"找不到正式 t1 物理場：{path}")
    field = np.asarray(np.load(path, allow_pickle=False), dtype=np.float64)
    if field.shape != GRID_SHAPE:
        raise ValueError(f"{filename} shape={field.shape}，預期 {GRID_SHAPE}")
    return field


def make_color_norm(field: np.ndarray, signed: bool) -> TwoSlopeNorm | None:
    """建立適合資料物理意義的色階。

    u/v 是帶方向的速度，正負值必須以 0 為共同中心；eta 是包含平均場的絕對水位，
    不強制以零為中心。NaN 不參與色階範圍計算。
    """

    if not signed:
        return None
    valid = np.isfinite(field)
    if not np.any(valid):
        raise ValueError("速度圖場沒有任何有限值")
    vmax = float(np.max(np.abs(field[valid])))
    if vmax == 0.0:
        vmax = 1.0
    return TwoSlopeNorm(vmin=-vmax, vcenter=0.0, vmax=vmax)


def apply_axes_style(ax: plt.Axes, lon: np.ndarray, lat: np.ndarray, title: str, t1_label: str) -> None:
    """套用五張圖共同的座標、比例與時間標籤，讓圖面可直接比較。"""

    ax.set_xlabel("經度 (°E)")
    ax.set_ylabel("緯度 (°N)")
    ax.set_xlim(float(lon[0]), float(lon[-1]))
    ax.set_ylim(float(lat[0]), float(lat[-1]))
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(f"{title}\n{t1_label}")
    ax.grid(color="#ffffff", linewidth=0.35, alpha=0.35)


def plot_scalar_field(
    *,
    field: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    spec: dict[str, object],
    t1_label: str,
    output_path: Path,
) -> dict[str, object]:
    """將單一 eta/u/v 物理圖場畫成帶經緯度的 PNG。

    無效位置保持透明遮罩；因此 10 m 的海床以下無資料格點會呈現空白，而不是被
    誤認為零速度。輸出值、單位與色階政策會回傳給 manifest 保存。
    """

    cmap = plt.get_cmap(str(spec["cmap"])).copy()
    cmap.set_bad(color="#f1f3f5", alpha=1.0)
    masked_field = np.ma.masked_invalid(field)
    norm = make_color_norm(field, bool(spec["signed"]))

    figure, axes = plt.subplots(figsize=(9.6, 6.8), constrained_layout=True)
    image = axes.pcolormesh(
        lon,
        lat,
        masked_field,
        shading="auto",
        cmap=cmap,
        norm=norm,
    )
    apply_axes_style(axes, lon, lat, str(spec["title"]), t1_label)
    colorbar = figure.colorbar(image, ax=axes, pad=0.02, shrink=0.92)
    colorbar.set_label(str(spec["colorbar"]))
    figure.savefig(output_path, dpi=220, facecolor="white")
    plt.close(figure)

    finite_count = int(np.isfinite(field).sum())
    return {
        "path": str(output_path),
        "shape": list(field.shape),
        "finite_count": finite_count,
        "nan_count": int(field.size - finite_count),
        "unit": str(spec["colorbar"]),
    }


def plot_vector_field(
    *,
    u: np.ndarray,
    v: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    depth_label: str,
    t1_label: str,
    output_path: Path,
) -> dict[str, object]:
    """將同一深度的 u/v 合成速度底圖與方向箭頭圖。

    色彩呈現 speed=sqrt(u²+v²)，箭頭呈現方向；u/v 的 NaN 位置同時從底圖和箭頭遮罩。
    ``QUIVER_STRIDE`` 只減少箭頭密度，不改變輸入物理值或 SVD 結果。
    """

    valid = np.isfinite(u) & np.isfinite(v)
    speed = np.where(valid, np.hypot(u, v), np.nan)
    masked_speed = np.ma.masked_invalid(speed)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(color="#f1f3f5", alpha=1.0)

    longitude_grid, latitude_grid = np.meshgrid(lon, lat)
    figure, axes = plt.subplots(figsize=(9.6, 6.8), constrained_layout=True)
    image = axes.pcolormesh(
        lon,
        lat,
        masked_speed,
        shading="auto",
        cmap=cmap,
    )
    apply_axes_style(
        axes,
        lon,
        lat,
        f"{depth_label} 流速與方向：正式 rank-20 t1 重建",
        t1_label,
    )

    # 使用 mask 讓無效格點不產生箭頭；stride 僅為了讓 102×152 格網的圖面可讀。
    sampled_u = np.ma.masked_invalid(u[::QUIVER_STRIDE, ::QUIVER_STRIDE])
    sampled_v = np.ma.masked_invalid(v[::QUIVER_STRIDE, ::QUIVER_STRIDE])
    arrows = axes.quiver(
        longitude_grid[::QUIVER_STRIDE, ::QUIVER_STRIDE],
        latitude_grid[::QUIVER_STRIDE, ::QUIVER_STRIDE],
        sampled_u,
        sampled_v,
        color="#17202a",
        pivot="mid",
        scale=28,
        width=0.0022,
    )
    axes.quiverkey(
        arrows,
        X=0.86,
        Y=1.035,
        U=0.5,
        label="0.5 m/s",
        labelpos="E",
        coordinates="axes",
    )
    colorbar = figure.colorbar(image, ax=axes, pad=0.02, shrink=0.92)
    colorbar.set_label("speed (m/s)")
    figure.savefig(output_path, dpi=220, facecolor="white")
    plt.close(figure)

    return {
        "path": str(output_path),
        "shape": list(u.shape),
        "finite_vector_count": int(valid.sum()),
        "nan_vector_count": int(valid.size - valid.sum()),
        "quiver_stride": QUIVER_STRIDE,
        "quiver_reference": "0.5 m/s",
    }


def plot_formal_t1(input_dir: Path, output_dir: Path) -> Path:
    """執行五張物理場圖與兩張 u/v 向量圖的唯讀繪圖流程。"""

    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_font = configure_report_font()

    metadata = read_json(input_dir / "metadata.json")
    if tuple(metadata["grid"]["shape"]) != GRID_SHAPE:
        raise ValueError(f"metadata grid shape 不是 {GRID_SHAPE}：{metadata['grid']['shape']}")
    lon, lat = load_grid_axes(input_dir / "feature_index_map.csv", GRID_SHAPE)
    t1_label = format_t1_utc(metadata)

    loaded: dict[str, np.ndarray] = {}
    scalar_outputs: list[dict[str, object]] = []
    for spec in FIELD_SPECS:
        name = str(spec["name"])
        field = load_field(input_dir, str(spec["filename"]))
        loaded[name] = field
        scalar_outputs.append(
            plot_scalar_field(
                field=field,
                lon=lon,
                lat=lat,
                spec=spec,
                t1_label=t1_label,
                output_path=output_dir / f"{name}_t1_rank20_map.png",
            )
        )

    vector_outputs = [
        plot_vector_field(
            u=loaded["u_surface"],
            v=loaded["v_surface"],
            lon=lon,
            lat=lat,
            depth_label="表層",
            t1_label=t1_label,
            output_path=output_dir / "uv_surface_t1_rank20_map.png",
        ),
        plot_vector_field(
            u=loaded["u_10m"],
            v=loaded["v_10m"],
            lon=lon,
            lat=lat,
            depth_label="10 m",
            t1_label=t1_label,
            output_path=output_dir / "uv_10m_t1_rank20_map.png",
        ),
    ]

    manifest = {
        "status": "formal_t1_two_layer_plots_complete",
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "source_t1_utc": t1_label,
        "grid_shape": list(GRID_SHAPE),
        "field_semantics": "formal top-20 rank reconstruction at first retained UTC",
        "report_font": report_font,
        "scalar_outputs": scalar_outputs,
        "vector_outputs": vector_outputs,
        "note": "本圖包只讀 t1 物理場；不重新建立兩年矩陣、不重跑 SVD。",
    }
    manifest_path = output_dir / "plot_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_dir


def main() -> None:
    """解析輸入／輸出目錄並執行正式 t1 圖面產生。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="正式 t1 衍生輸出目錄，預設為 work/formal_t1_two_layers。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="PNG 圖面輸出目錄；省略時寫入 input-dir/figures。",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or args.input_dir / "figures"
    print(plot_formal_t1(args.input_dir, output_dir))


if __name__ == "__main__":
    main()
