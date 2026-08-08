"""OCM paired native 資料的垂向取樣與水平重心內插共用工具。

本模組只處理幾何與物理欄位轉換，不負責 SVD、輸出目錄命名或任何特定分析產品。
水柱產品會以這些工具把 native ``hvel/zcor`` 的 source-node 欄位轉成指定的物理
水深，再依前處理發布的三角形頂點與重心權重回填到規則經緯度格網；因此此處的輸入
輸出契約可被不同分析流程共用，而不會把某個已停用產品的命名空間帶入新產品。
"""

from __future__ import annotations

import numpy as np


def _require(condition: bool, message: str) -> None:
    """以一致例外型別檢查幾何內插的資料契約。

    內插函式接收的是由前處理產生的 NumPy 陣列；shape 或有限值契約一旦不符，繼續
    計算可能會產生看似合理但實際錯配的速度場。因此共用工具在進入向量化運算前，
    直接以 ``ValueError`` 回報可定位的輸入問題，而不默默修正或填補來源資料。
    """

    if not condition:
        raise ValueError(message)


def interpolate_velocity_to_target_z(
    hvel: np.ndarray,
    zcor: np.ndarray,
    target_z_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把 native source-node 水平速度線性取樣至指定物理 ``z``，禁止外插。

    參數
    ------
    hvel:
        浮點陣列，維度為 ``(time, source_node, layer, component)``；前兩個分量依
        OCM 契約代表東向 ``u`` 與北向 ``v``，其餘分量不參與此次取樣。
    zcor:
        浮點陣列，維度為 ``(time, source_node, layer)``，表示每個時次、source node
        與垂向層的實際 z 座標（m）。它必須與 ``hvel`` 的前三維逐值對齊。
    target_z_m:
        目標物理 z 座標（m）。水面下深度通常以負值表示，例如水下 10 m 為 ``-10``。

    回傳
    ------
    tuple[np.ndarray, np.ndarray, np.ndarray]
        ``u``、``v`` 與上下包夾層距離，前三者 shape 均為 ``(time, source_node)``；
        若該柱沒有同時有限且包夾目標 z 的上下層，三個輸出對應位置均為 ``NaN``。
        若目標正好落在有效層，直接採該層速度，包夾距離為 0。

    限制
    ------
    不假設 layer index 已排序，也不以最近單側層替代上下包夾；這能避免在海面以上或
    海床以下創造未被 native 資料支持的速度。缺值也不填 0，讓後續水柱流程依有效遮罩
    決定是否保留該格點與時次。
    """

    _require(
        hvel.ndim == 4 and hvel.shape[-1] >= 2,
        "垂向取樣的 hvel 必須是 (time,node,layer,component>=2)",
    )
    _require(
        zcor.shape == hvel.shape[:3],
        "zcor 必須與 hvel 的 time/node/layer 完全對齊",
    )
    _require(np.isfinite(target_z_m), "垂向取樣 target_z_m 必須有限")

    # 只取 u/v 並提升為 float64，讓後續線性內插不受來源 float32 的中間乘法精度限制；
    # 這裡不修改原始 memory-map，所有計算結果都會寫入新的陣列。
    velocity = np.asarray(hvel[..., :2], dtype=np.float64)
    physical_z = np.asarray(zcor, dtype=np.float64)
    usable = np.isfinite(physical_z) & np.all(np.isfinite(velocity), axis=-1)

    # 每個 time/source-node 垂向柱分別找「目標以下最接近」與「目標以上最接近」的層。
    # 以 -inf/+inf 暫代無效候選，argmax/argmin 才能在不假設 layer 排序的前提下向量化。
    below_candidates = usable & (physical_z <= target_z_m)
    above_candidates = usable & (physical_z >= target_z_m)
    has_bracket = np.any(below_candidates, axis=-1) & np.any(above_candidates, axis=-1)
    below_index = np.argmax(np.where(below_candidates, physical_z, -np.inf), axis=-1)
    above_index = np.argmin(np.where(above_candidates, physical_z, np.inf), axis=-1)

    z_below = np.take_along_axis(physical_z, below_index[..., None], axis=-1)[..., 0]
    z_above = np.take_along_axis(physical_z, above_index[..., None], axis=-1)[..., 0]
    uv_below = np.take_along_axis(
        velocity,
        below_index[..., None, None],
        axis=2,
    )[..., 0, :]
    uv_above = np.take_along_axis(
        velocity,
        above_index[..., None, None],
        axis=2,
    )[..., 0, :]

    # 目標恰落在某一層時避免除以 0；其他位置依上下包夾 z 做線性比例內插。
    span = z_above - z_below
    exact = has_bracket & (np.abs(span) <= np.finfo(np.float64).eps * 16.0)
    nonzero = has_bracket & ~exact
    result = np.full((*physical_z.shape[:2], 2), np.nan, dtype=np.float64)
    result[exact] = uv_below[exact]
    fraction = np.zeros_like(span)
    fraction[nonzero] = (target_z_m - z_below[nonzero]) / span[nonzero]
    result[nonzero] = uv_below[nonzero] + fraction[nonzero, None] * (
        uv_above[nonzero] - uv_below[nonzero]
    )
    bracket_span = np.where(has_bracket, span, np.nan)
    return result[..., 0], result[..., 1], bracket_span


def _horizontal_barycentric_interpolate(
    node_values: np.ndarray,
    local_vertices: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """依前處理發布的三角形頂點與重心權重回填規則格網。

    ``node_values`` 代表已選取 source-node 的時間序列，shape 為
    ``(time, selected_source_node)``；``local_vertices`` 與 ``weights`` 則是每個
    規則格網 cell 的三個局地 source-node 索引與重心權重，shape 為 ``(lat, lon, 3)``。
    三個支撐節點只要有一個索引無效、權重非有限或速度為 NaN，該格就維持 NaN。這個
    保守政策避免跨越無資料三角形或把陸地／缺測欄位製造成有效海流。
    """

    _require(
        node_values.ndim == 2,
        "水平內插 node_values 必須是 (time, selected_source_node)",
    )
    _require(
        local_vertices.ndim == 3
        and local_vertices.shape[-1] == 3
        and weights.shape == local_vertices.shape,
        "水平內插 vertices/weights 必須是 (lat,lon,3)",
    )

    supported = np.all(local_vertices >= 0, axis=-1) & np.all(np.isfinite(weights), axis=-1)
    safe_vertices = np.where(local_vertices >= 0, local_vertices, 0)
    # 先使用安全索引完成 gather，再以 supported 遮罩清除不具備完整三角形支撐的格點；
    # 這避免負索引在 NumPy 中意外取到最後一個 source node。
    gathered = node_values[:, safe_vertices]
    finite = supported[None, ...] & np.all(np.isfinite(gathered), axis=-1)
    result = np.full((node_values.shape[0], *supported.shape), np.nan, dtype=np.float64)
    weighted = np.sum(gathered * weights[None, ...], axis=-1)
    result[finite] = weighted[finite]
    return result
