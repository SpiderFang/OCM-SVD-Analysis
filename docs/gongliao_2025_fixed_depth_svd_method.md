# 2025 貢寮固定深度流速—海面高度 SVD 方法

## 1. 研究問題與分析層位

本分析延續既有貢寮 focus bbox 表層 `u/v/eta` SVD，回答：

> 在相同研究區、時間樣本與水平格點下，表層及固定 `z=-5、-10、-20 m` 的水平流速，
> 分別如何與同一個自由水面高度場共同變化？

四個層位各自建立獨立三變數 SVD：

| 層位 | SVD 變數 | `eta` 語意 |
|---|---|---|
| 表層參考 | `u_surface, v_surface, eta` | 同時次自由水面高度 |
| `z=-5 m` | `u(-5), v(-5), eta` | 同一份自由水面高度，不是 -5 m 的標量 |
| `z=-10 m` | `u(-10), v(-10), eta` | 同一份自由水面高度，不是 -10 m 的標量 |
| `z=-20 m` | `u(-20), v(-20), eta` | 同一份自由水面高度，不是 -20 m 的標量 |

固定深度 family 不包含近底／HAB，也不把 48 個最大垂向 layer 全部堆入同一個三維 SVD。

## 2. `eta` 如何取得

`eta` 不在 `hvel/zcor` 的垂向 layer 中。來源鏈為：

```text
raw CWA-OCM / SCHISM
elev(time, source_node) [m]
    ↓ 裁切、填充值轉 NaN
ocm_native/<domain>/months/<YYYYMM>/elev.npy
    ↓ 與表層 u/v 相同的 Delaunay vertices 與重心權重
ocm_surface/<domain>/months/<YYYYMM>/eta_m.npy
    shape = (time, lat, lon)
```

固定深度管線直接讀取已驗收的 `eta_m.npy`，不從 `zcor`、最上層高度、潮汐公式或固定
深度速度反推。paired surface/native 的 `time_utc_ns.npy` 必須逐值完全相同；對 2025 年
7 月前 24 筆時間標籤，管線採用專案端預先登錄的 +24 小時時間軸正規化假設。此非原始
NetCDF 資料提供者的更正或確認，且只在分析記憶體同步套用，不改變 `eta` 或速度數值。

上游 `valid_mask_surface.npy` 只表示表層 `u/v/surface_z` 有效，不能證明 -5、-10 或
-20 m 有上下包夾層。固定深度逐時有效條件因此重新計算為：

```text
mask_static
AND focus bbox cell center
AND finite(u_fixed_z)
AND finite(v_fixed_z)
AND finite(eta_m)
```

## 3. 固定 `z` 垂向內插

每個時間、source node、目標 `z_t` 都只使用 `zcor/u/v` 同時有限的層。令：

- `z_b`：所有 `z <= z_t` 中最接近 `z_t` 的有效層；
- `z_a`：所有 `z >= z_t` 中最接近 `z_t` 的有效層。

只有兩者都存在時才計算：

$$
\alpha = \frac{z_t-z_b}{z_a-z_b},
\qquad
\mathbf{u}(z_t) = \mathbf{u}(z_b)
                 + \alpha[\mathbf{u}(z_a)-\mathbf{u}(z_b)].
$$

若 `z_a=z_b=z_t`，直接採用該層速度。缺少任一包夾層時輸出 NaN，不做：

- 海床以下外插；
- 海面以上外插；
- 只有單側有效層的常數延伸；
- 以 0 代表無效水柱；
- 以固定 layer index 代替固定物理深度。

source-node 內插完成後，使用 surface grid 已發布的 `source_vertices.npy` 與
`source_weights.npy` 水平重採樣至 1 km 規則格。三個支撐節點必須全有限，否則目標格
保持 NaN。

## 4. 共同遮罩與共同時間

本設定採 `intersection_with_surface`。先分別計算表層、-5、-10、-20 m 的三變數逐時
有效遮罩與全年有效率，再建立：

```text
shared_valid_mask =
    analysis_geometry_mask
    AND mask_static
    AND all(level_valid_fraction >= 0.95)
```

每個層位只在相同 cell 內處理最多兩個時間步的有界短缺值；最後再保留所有層位、所有
共同格點都完整的時間交集。這個保守政策的目的，是避免兩個模態差異其實來自不同空間
範圍或不同時間母體。

既有完整表層 run 使用 272 個共同表層格點。固定深度 family 的表層參考會因 -20 m
coverage 而使用較小交集，因此：

- 既有表層 run：描述完整可用表層海域；
- family 表層參考：只服務相同樣本的垂向模態比較；
- 不可把兩者的解釋變異量當成同一母體直接相減。

## 5. SVD 正規化與輸出

四個層位都沿用目前表層方法：

- 各格點沿共同時間軸去平均；
- `u/v` 共用面積加權向量 RMS；
- `eta` 使用自己的面積加權 RMS；
- 三個分量各乘 `sqrt(cell_area_m2)`；
- 以空間協方差 `eigh` 回復薄型 SVD、PC 與解釋變異；
- 沿用同一 anchor 與 sign convention。

每個 level 都輸出 `mode_*`、`regression_*`、`pc*`、解釋變異、平均場、有效率及
imputation mask。固定深度另輸出：

```text
vertical_bracket_span_m.npy  # (time, lat, lon)
```

它表示上下 `zcor` 包夾距離經相同水平權重內插後的 QC 值。包夾距離越大，代表固定深度
速度由較寬的垂向間距推得；這不等於模式 layer 厚度，也不能自行證明局地底邊界層已被
解析。

## 6. 學術依據與適用限制

- [Zhang et al. (2016)](https://doi.org/10.1016/j.ocemod.2016.05.002) 說明 SCHISM
  彈性垂向網格；支持不可把相同 layer index 當成相同物理深度。
- [Faucher et al. (2002)](https://doi.org/10.1029/2000JC000690) 說明垂向 SVD 可由
  模式輸出建立，且選擇 depth／isopycnal coordinate 會改變物理解讀。
- [Giarolla et al. (2005)](https://doi.org/10.1029/2004GL022206) 以觀測與模式固定深度
  流速剖面 SVD 描述潛流變動。
- [Liu and Weisberg (2005)](https://doi.org/10.1029/2004JC002786) 將多站、多層位
  `u/v` 組成向量 SVD，直接支持比較表層、中層與近底流速模態。
- [Oey et al. (2004)](https://doi.org/10.1029/2004JC002345) 顯示垂向座標與空間取樣差異
  會改變流速 SVD，因此本 family 必須明載共同遮罩與時間政策。

這些文獻支持固定物理深度與分層流速 SVD 的方法選擇，但不自動驗證本次 OCM 的
垂向 datum、模式準確度或物理歸因。正式報告前仍須確認：

1. `zcor` 的單位、正方向與垂向基準；
2. `hvel` 的 east/north 分量與 m s⁻¹ 契約；
3. -20 m 共同格點數與垂向包夾距離是否足夠；
4. 各模態是否通過低通、季節、年份與 bootstrap 敏感度分析。

## 7. 執行命令

```bash
export UV_CACHE_DIR=/home/mustlab/work/uv-cache/ocm-svd-analysis
export UV_PROJECT_ENVIRONMENT=/home/mustlab/work/venvs/ocm-svd-analysis-py312
export UV_MANAGED_PYTHON=1
export MPLCONFIGDIR=/home/mustlab/work/matplotlib-cache/ocm-svd-analysis
export PYTHONDONTWRITEBYTECODE=1

uv python install 3.12.13
uv sync --frozen --python 3.12.13 --managed-python

uv run --frozen --no-sync --python 3.12.13 ocm-svd-fixed-depth \
  --config configs/gongliao_fixed_depth_svd_available_2025.json \
  --native-root "$OCM_NATIVE_ROOT" \
  --surface-root "$OCM_SURFACE_ROOT" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --allow-partial-months
```

先以另一個明確的 smoke-test `analysis_label_vN` 執行一個月試算，可分辨 native I/O、
垂向內插與 SVD 求解耗時。固定深度科學 run 一律只輸出陣列與 metadata；圖面另由
`ocm-svd-fixed-depth-replot` 發布，所以不再需要 `--no-figures`。正式全年 run 不應覆寫
smoke test 或既有完整表層成果。

`UV_PROJECT_ENVIRONMENT` 指向專案專用環境，而 `UV_MANAGED_PYTHON=1` 與明確的
`--python 3.12.13` 阻止 uv 回退到系統或 Anaconda Python。即使 shell 的 `(base)`
提示仍存在，也不影響實際解譯器；可用以下命令核對：

```bash
uv run --frozen --no-sync --python 3.12.13 python -c \
  'import sys, numpy; print(sys.executable); print(sys.version); print(numpy.__version__)'
```

## 8. 2026-07-31 全年 SERVER 驗證

全年 run 已在
`/home/mustlab/Workspace/OCM-SVD-Analysis` 以 uv 管理的 Python 3.12.13、NumPy 2.5.1
完成；輸入仍留在
`/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/{ocm_native,ocm_surface}`，
未搬到本機。可讀、不可覆寫的固定深度版本 ID 為
`gongliao_focus_bbox_surface_reference_fixed_z_005_010_020_u_v_eta_available_2025_v1`。
完整科學內容 SHA-256 保存在 `metadata.json > science_provenance_sha256`，不再附加於
資料夾名稱。固定深度位於 `fixed_depth_svd/`，完整表層位於 `svd/`，兩者各自獨立。

驗證結果：

- 共同海域 206 格，8,514 個來源可得時次中共同保留 8,442 個（99.15%）。
- 四層 `time_utc_ns.npy`、`shared_valid_mask.npy` 與 `mean_eta_m.npy` 逐值完全相同。
- 四層第一模態解釋變異量依序為 52.66%、54.00%、55.47%、58.09%。
- 全程 1:57:54，峰值 RSS 15.13 GiB，無 swap。
- 月檔 I/O 與垂向內插 7,030.42 秒；共同遮罩 1.43 秒；四組 SVD 求解與衍生場
  合計 1.82 秒。因此目前成本幾乎全是 NFS 分散索引讀取，不是 SVD 線性代數。
- 整理後的科學 family 為 91 個檔案、111.12 MiB，包含 85 個 NPY 與 6 個 JSON；
  科學陣列未改值，SERVER 舊 renderer 圖面已從 family 移出。正式 v6 圖只存在獨立的
  `fixed_depth_svd_figure_bundles/`，避免和完整表層或舊比例尺版本混用。

此次 focus bbox 需從 67,593 個 source nodes 取 712 個節點，分布於 291 個不連續區段。
雖然最終節點只占 1.05%，mmap 對 NFS 的分頁讀取仍造成明顯 I/O 放大。後續若要加速，
應先建立只包含這些連續 source-node 區段的小型 paired native 月快取；單純提高 SVD
執行緒數不會顯著縮短總耗時。

## 9. 正式圖包、向量比例尺與 coverage QC

本次維持來源 family 的 206 格共同遮罩，不回填被排除的 66 格。為使固定深度圖面延續
既有表層 `academic_report_ready_v6` 規格，另由已發布陣列建立唯讀衍生圖包：

[`academic_report_ready_v6`](/Users/mustlab/Workspace/OCM-SVD-Analysis/outputs/server_results/2026-07-31/fixed_depth_svd_figure_bundles/gongliao_focus_bbox_surface_reference_fixed_z_005_010_020_u_v_eta_available_2025_v1/academic_report_ready_v6)

每個層位的平均場與 SVD 空間圖均有三種配對資產：

1. `*_report.*`：不含向量比例尺的標準白底主圖；
2. `*_vector_scale_transparent.*`：只含該圖 q95 箭頭與單位的透明後製素材；
3. `*_with_vector_scale.*`：右下角已嵌入同比例尺，可直接放入報告。

固定深度空間圖標題會標示 `-5 m`、`-10 m` 或 `-20 m` 流，以防圖檔離開目錄後混淆
u/v 的物理層位；這只改圖面文字，不改回歸場、PC、解釋變異量或共同的自由水面高度
`eta`。

圖包根目錄的 `fixed_depth_shared_coverage_qc_report.*` 逐層呈現 95% 年度三變數有效率
門檻：

| 層位 | 達標格 | 未達門檻格 |
|---|---:|---:|
| 共同遮罩表層參考 | 272/272 | 0 |
| `z=-5 m` | 272/272 | 0 |
| `z=-10 m` | 272/272 | 0 |
| `z=-20 m` | 206/272 | 66 |

因此四層共同交集是 206/272 格。QC 圖的橙色代表仍在 analysis geometry 內、但該層
未達年度有效率門檻的海洋候選格；暖灰色才是版本化 OSM 陸地。這張圖只交代遮罩來源，
不顯示或改寫流速、`eta` 或 SVD 幅度。

重繪命令如下；它不讀 paired native/surface cache、不做垂向內插，也不重新求解 SVD：

```bash
uv run --frozen --no-sync --python 3.12.13 ocm-svd-fixed-depth-replot \
  --run-dir outputs/server_results/2026-07-31/fixed_depth_svd/gongliao_focus_bbox_surface_reference_fixed_z_005_010_020_u_v_eta_available_2025_v1 \
  --output-root outputs/server_results/2026-07-31
```

本機實跑重繪約 19.53 秒，其中四層圖面約 18.67 秒、coverage QC 約 0.49 秒；來源
family 的 `metadata.json` SHA-256 在重繪前後保持一致。
