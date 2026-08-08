# OCM SVD Analysis

本專案延續 `OCM-Data-Preprocessing` 已驗收的 `ocm_surface` 與 paired `ocm_native`
`.npy` flow-domain 快取，建立可重跑的流場 SVD 成果。前處理專案是「flow domain、候選
框脈絡、遮罩與 provenance」的唯一權威；本專案只依其版本化定義讀取資料、做 SVD 與輸出
衍生結果。程式絕不讀取原始 SCHISM NetCDF、不重複前處理或複製 flow cache，也不會把缺值、
陸地或無效流速填成 0。

「SVD」是本專案所有設定、CLI、檔名、圖面與文件的固定名稱。既有表層產品保留其歷史的
空間協方差求解器；本次新增的後灣完整 flow-domain 水柱產品則明確使用**直接 SVD**：小型
矩陣採 `numpy.linalg.svd`，大型矩陣採同一個加權矩陣的 PROPACK 直接奇異三元組求解。新產品
不建立 `X.T @ X`、`X @ X.T` 或任何「由協方差回復 SVD」的結果，也不以此類等價敘述取代
直接求解的數值證據。

## 後灣完整 flow-domain：表層至 50 m 的單一聯合直接 SVD

本次研究需求所述的「水深 50 m，分 10、20、30、40」在實作上固定為六個速度層：已發布的
**表層**、固定水下 **10、20、30、40、50 m**。表層不是 native 資料中的固定 datum `z=0`；
它直接沿用前處理已選取之 `ocm_surface` 表層 `u/v`。固定水下層則由 paired
`ocm_native/hvel.npy` 與 `zcor.npy` 在每個時次、每個 source node 找有限的上下包夾層作線性
內插；沒有包夾時維持 `NaN`，不向海面或海床外插。

報告不需要揭露 `ocm_surface/ocm_native` 的內部資料分工；研究上可表述為：「以表層及
固定水下 10–50 m 的流速，連同同時次的一份自由水面高度，建立一個聯合 SVD。」內部來源的
分工只用於確保資料物理意義正確：

| 研究變數 | 實作資料來源 | 進入狀態向量的次數 |
| --- | --- | --- |
| 表層 `u/v` | 已發布 `ocm_surface/u_surface_mps.npy`、`v_surface_mps.npy` | 各一次 |
| 水下 10、20、30、40、50 m `u/v` | paired `ocm_native/hvel.npy`、`zcor.npy` 固定深度內插 | 每個深度的 `u/v` 各一次 |
| 自由水面高度 `eta` | 已發布 `ocm_surface/eta_m.npy` | **全矩陣只一次** |

`eta` 是二維自由水面場，沒有垂向層；因此不得在六個速度深度重複。設第 `l` 個速度層保留
`P_l` 個格點、`P_eta` 為 eta 格點，單一時次的狀態向量依序為

$$
q(t)=[\eta,\ u_0,\ u_{10},\ u_{20},\ u_{30},\ u_{40},\ u_{50},\ v_0,\ v_{10},\ v_{20},\ v_{30},\ v_{40},\ v_{50}]^T .
$$

六個 `u/v` 層與唯一 eta 欄位會依研究需求指定排列串成**同一個** `A=(feature, time)` 加權距平矩陣，
再依設定的模態數求同一組空間模態與共同 PC；並非每個深度各做一次 SVD。流速採
`sqrt(cell_area × 垂向梯形權重)`（`5, 10, 10, 10, 10, 5 m`）與共用體積加權 RMS；eta 僅採
`sqrt(cell_area)` 與面積加權 RMS，因此不會因層數被人為加權六倍。

完整 flow domain 的矩陣可能超出 RAM。流程先將原始特徵寫入 float32 disk-backed 暫存，再
建立無缺值的 float64 `A=(feature,time)` 加權矩陣；若薄型 dense SVD 的保守 RAM 估計超過設定預算，改用
`scipy.sparse.linalg.svds(..., solver="propack")` 的 `LinearOperator` 直接對該矩陣求設定模態數
的奇異三元組。它只做分塊 `A @ v` 與 `A.T @ u`，不建立 normal matrix；每次求解均寫出左右
殘差、正交誤差、求解器與資源用量到 `metadata.json`。對大型 memory-map 矩陣，驗證用的獨立
分塊乘法可能產生數個 `1e-9` 的浮點累加殘差，因此發布檢查採設定門檻與明載 `1e-8` streaming
數值地板中的較寬者，並額外要求正交性誤差不超過 `1e-10`；設定值、有效門檻、每次嘗試與實測
殘差全都保留在 metadata。此地板不改變 `A`、權重、變數或直接 SVD 演算法。因此 RAM 不足時會
切換為可驗證的直接 disk-backed 方法，而不是停止、降階成協方差法或暗中改做分層 SVD。

native I/O 策略必須在 `parallel_execution.native_block_read_strategy` 顯式寫出，並同步記錄於
預檢及正式 `metadata.json`。後灣兩年正式設定採 `selected_nodes_fancy_index`：只 materialize
水平內插所需的 6,984 個 native node，不讀取未使用的 17,463 個 node；這是以 SERVER NFS
實測決定的吞吐最佳化。另一個可用策略 `contiguous_full_source_axis_then_select` 會先讀完整
source-node 軸後選出子集，僅供其他 domain 在 I/O 實測較快時明確採用。兩種策略都只改變檔案
讀取方式，不改變 `hvel/zcor` 數值、固定深度內插、有效遮罩、資料矩陣或 SVD，且不會在執行中
暗中切換。

原始 NaN 不補 0、不插補。流程會先依每個深度的實際有效率保留配對 `u/v` 欄位，再以所有保留
欄位都有限的共同時次求解；若設定門檻造成共同時間交集過小，會收縮到全時段有限的特徵，而
非創造資料。每層遮罩可以不同，但全部保留欄位仍屬同一個 SVD。這是「表層至 50 m 的採樣
層聯合分析」，不宣稱每一格都完整代表連續水柱。

正式設定為
[`houwan_nmmba_flow_domain_water_column_svd_available_2024_2025.json`](configs/houwan_nmmba_flow_domain_water_column_svd_available_2024_2025.json)。
它只涵蓋後灣／海生館完整 `houwan_nmmba_cache_v3`，目前設定先求取 100 個聯合模態，預設
只產出前 20 個模態圖面。每個已繪製模態
分別輸出表層、10、20、30、40、50 m 六張獨立速度空間圖、唯一 eta 空間圖與一張獨立
標準化 PC 時序圖；另有一張獨立解釋變異圖與七張獨立 feature coverage QC 圖。正式圖面
不再產生把多個深度塞在同一張 3×3 subplot 的 `mode_01.png` 類複合圖。完成、檢查
metadata 與圖面後才可由研究團隊明確決定是否對其餘 flow domains 建立各自的新版本；本次
入口不會自動擴大成四區或六區批次。

### 研究需求指定排列：`eta → 全部 u → 全部 v` 的 feature×time 矩陣與回填圖場

矩陣示意圖中每個時間是一個直式狀態向量，所有時間向量並排，因此正式求解器直接採用
`A=(feature, time)`；原始時間主序檔只作為 I/O 中間格式，不是 SVD 矩陣。正式資料、權重與
模態數均由設定檔控制；目前後灣正式設定求取 100 組，圖面預設只畫前 20 組，因此後續可
以重繪直接產出的第 21–30 組，不必重新讀取兩年資料或重建加權矩陣。
為避免在答辯時將「原始物理值」誤當成「送入 SVD 的加權距平」，白板可依下列順序畫出。

```text
步驟 1：對每個速度深度建立最終有效格點清單（不是把 X/NaN 填成 0）

  C_eta                 : eta 可用的表層格點
  C_0, C_10, ..., C_50  : 各速度深度可用的格點；每層可不同

  陸地、海床以下、固定深度無上下 zcor 包夾、或未通過有效率門檻的位置
  --> 不配置 feature row；日後畫圖時保留 NaN / X。


步驟 2：在單一 UTC 時刻 t_j 依指定排列取物理狀態欄向量 q_phys(t_j)

                     ┌ eta(c_1,t_j)                 c_1 ∈ C_eta
                     │ eta(c_2,t_j)
                     │ ...
                     │ eta(c_Neta,t_j)               eta 只放一次
                     ├ u(surface,c_1,t_j)            c_1 ∈ C_0
                     │ ...
                     │ u(50m,c_N50,t_j)              所有深度的 u 連續排列
 q_phys(t_j) =       ├ v(surface,c_1,t_j)            c_1 ∈ C_0
                     │ ...
                     │ v(50m,c_N50,t_j)              同樣的深度／格點順序排列
                     └


步驟 3：先逐 feature 去時間平均、再加上既定物理權重與 RMS 標準化

  a(t_j)[p] = scale[p] × ( q_phys(t_j)[p] - mean[p] )

  u/v 的 scale = sqrt(cell_area × 該層垂向權重) / velocity_RMS
  eta 的 scale = sqrt(cell_area) / eta_RMS

  這個 a(t_j) 才是 SVD 的欄向量；mean 與 scale 會另存，所以可還原物理單位。


步驟 4：將所有 UTC 時間欄並排，得到指定排列版加權距平矩陣 A

                  time →       t_1       t_2       t_3       ...     t_T
                             ┌───────────────────────────────────────────────┐
  eta rows                   │ a_eta(1,t_1)  a_eta(1,t_2)  a_eta(1,t_3)  ... │
  (eta only once)            │       ...            ...            ...       │
  ─────────────────────────  ├───────────────────────────────────────────────┤
  u rows: surface,10,...50m  │ a_u(surface,1,t_1)                  ...        │
                             │       ...                                      │
  ─────────────────────────  ├───────────────────────────────────────────────┤
  v rows: surface,10,...50m  │ a_v(surface,1,t_1)                  ...        │
                             │       ...                                      │
                             └───────────────────────────────────────────────┘
                                      A.shape = (F features, T retained times)


步驟 5：直接求設定模態數的奇異三元組（後灣目前設定為 100 組）

  A_r = U_A[:,1:r] × diag(sigma_1,...,sigma_r) × Vh_A[1:r,:]

  r = svd.requested_mode_count；U_A 的每一欄是一個聯合空間模態，列順序就是 eta -> all u -> all v。
  PC(r,t) = sigma_r × Vh_A(r,t)：第 r 個模態的時間係數。圖面只由 figures.mode_count 決定。


步驟 6：由右側的 compact U_A 欄向量畫回左側六層圖

  第 r 模態的第 p 個物理 loading
       e_r[p] = U_A[p,r] / scale[p]

  feature_index_map.csv 的第 p 列
       (component, depth, grid_row, grid_col)
       eta, --,  i, j  --> eta_map[r,i,j] = e_r[p]
       u,   20m,i, j  --> u_map[r,20m,i,j] = e_r[p]
       v,   20m,i, j  --> v_map[r,20m,i,j] = e_r[p]

  沒有 feature row 的位置 --> 保持 NaN / 畫 X，不是 0。
```

對於舊版已發布、仍使用 `X=(time, feature)` 的成果，令 `P` 為把舊列順序換成指定排列的
置換矩陣，則

$$
A=P X^T,\qquad X_{20}=U_X\Sigma_{20}V_{h,X}
\quad\Longrightarrow\quad
A_{20}=(P V_X)\Sigma_{20}U_X^T .
$$

因此 `U_A=P V_X`、`Vh_A=U_X.T`。這是舊成果的精確轉置／重排，不重新讀取兩年
`ocm_surface/ocm_native`、不建立協方差矩陣、也不以另一個近似演算法取代直接 SVD。新版本
正式求解器已直接產生 `A`，因此不需要再做這個相容性轉換。

已完成的水柱 run 可用下列只讀入口發布指定排列成果；它會雜湊來源檔、拒絕覆寫，並將由
`U_A` 回填的圖場同來源模式逐值 round-trip 比較（門檻 `1e-12`）：

```bash
SOURCE_RUN=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-06/water_column_svd/houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1
HOST_LAYOUT_ROOT=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-07_host_layout

uv run --frozen --no-sync --python 3.12.13 ocm-svd-water-column-host-layout \
  --run-dir "$SOURCE_RUN" \
  --output-root "$HOST_LAYOUT_ROOT"
```

```text
water_column_svd_host_layout/<source-analysis-label>/
└── eta_u_all_depths_v_all_depths_feature_by_time_v1/
    ├── left_singular_vectors_weighted.npy      # U_A, (feature, r)
    ├── right_singular_vectors_time.npy         # Vh_A, (r, time)
    ├── singular_values.npy                      # Sigma, (r,)
    ├── pc.npy                                   # Sigma × Vh_A, (r, time)
    ├── feature_scale.npy / feature_mean_physical.npy
    ├── feature_index_map.csv                    # 每一矩陣列 -> eta/u/v、深度、格點、經緯度
    ├── roundtrip_mode_u_mps_per_raw_pc.npy     # 由 U_A 回填，(20, 6, lat, lon)
    ├── roundtrip_mode_v_mps_per_raw_pc.npy     # 由 U_A 回填，(20, 6, lat, lon)
    ├── roundtrip_mode_eta_m_per_raw_pc.npy     # 由 U_A 回填，(20, lat, lon)
    └── metadata.json                            # 矩陣式、來源雜湊、殘差對應、round-trip 證據
```

### 正式成果的兩層 t1 展示欄

若答辯時只需要展示兩個正式深度層，可由已發布水柱 run 的 `mean`、physical mode 與第一個
正式 PC 只讀建立一欄；預設選正式表層與 10 m 的**完整 102×152 格網及其有效遮罩**，不使用
toy 數值，也不把正式格網裁成 3×3：

```bash
uv run python3 scripts/extract_formal_t1_whiteboard_demo.py \
  --run-dir /home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-06/water_column_svd/houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1 \
  --output-dir work/formal_t1_two_layers
```

這個展示欄的物理值是正式前 20 模態在第一個保留 UTC 時次的 rank-20 重建：
`mean + physical_mode × PC(t1)`。列順序為 `eta → u(surface) → u(10m) → v(surface) →
v(10m)`，回填陣列仍維持正式 `102×152` 格網；`feature_index_map.csv` 只列入正式 mask
為 true 的格點。單欄 SVD 只有一個非零奇異值，故這是正式結果的排列／回填展示，不取代完整
兩年 20 模態的正式成果，也不宣稱是未截斷原始 t1 觀測欄。

五個 `roundtrip_*_physical_t1_rank20.npy` 已是可直接繪圖的完整物理格網。若要以正式
經緯度畫出五張單場圖及表層／10 m 兩張 u/v 向量圖，可執行
[`scripts/plot_formal_t1_two_layers.py`](scripts/plot_formal_t1_two_layers.py)；詳細輸入、
輸出檔名與 `NaN` 遮罩規則見 [`work/formal_t1_two_layers/README.md`](work/formal_t1_two_layers/README.md)。

## 六個分析單元與跨專案資料契約

分析單元定義固定在前處理專案的
[`ocm_svd_analysis_units_v1.json`](/Users/mustlab/Workspace/OCM-Data-Preprocessing/configs/ocm_svd_analysis_units_v1.json)。
每一份本專案設定都保存該檔案的 SHA-256；分析邊界、核定狀態或 coverage 門檻一旦更改，
必須在前處理專案建立新版本，不能只在 SVD 專案局部修改 bbox。

| 主要 SVD 分析單元 | 狀態 | flow domain | 2024–2025 正式設定 |
| --- | --- | --- | --- |
| 龜山島西側海域 | candidate | `northeast_taiwan_common_cache_v3` | `guishan_surface_svd_available_2024_2025.json` |
| 貢寮海域 | candidate | `northeast_taiwan_common_cache_v3` | `gongliao_surface_svd_available_2024_2025.json` |
| 新竹沿岸 | candidate | `hsinchu_cache_v3` | `hsinchu_surface_svd_available_2024_2025.json` |
| 後灣／海生館 | candidate | `houwan_nmmba_cache_v3` | `houwan_nmmba_surface_svd_available_2024_2025.json` |
| 北竿海域 | approved | `lienchiang_common_cache_v3` | `beigan_surface_svd_available_2024_2025.json` |
| 南竿海域 | approved | `lienchiang_common_cache_v3` | `nangan_surface_svd_available_2024_2025.json` |

北竿與南竿是兩個核定的主要 SVD 分析區。舊有北竿 3 個、南竿 4 個極小候選框保留
為各主區內的附屬局地位置／受體／局地統計視窗，**不**各自產生五個「空間模態」成果。
原因與邊界記錄見前處理專案的
[`SVD_ANALYSIS_UNITS_V1.md`](/Users/mustlab/Workspace/OCM-Data-Preprocessing/docs/flow_domain_cache/SVD_ANALYSIS_UNITS_V1.md)。

輸入根目錄必須是前處理產物的 `ocm_surface/`，其下具有：

```text
ocm_surface/
└── northeast_taiwan_common_cache_v3/
    ├── grid/
    │   ├── lon.npy
    │   ├── lat.npy
    │   ├── cell_area_m2.npy
    │   └── mask_static.npy
    └── months/
        └── <YYYYMM>/
            ├── metadata.json
            ├── time_utc_ns.npy
            ├── u_surface_mps.npy
            ├── v_surface_mps.npy
            ├── eta_m.npy
            └── valid_mask_surface.npy
```

每區讀取時會對 analysis bbox 向外多取一格作 memory-map 緩衝，但新六區設定強制以封閉
`analysis_bbox` 的 **cell center** 遮罩再次過濾；因此讀取緩衝格不會進入 SVD 狀態
向量。輸出中的 `analysis_geometry_mask.npy`、`valid_mask.npy` 與 `metadata.json` 可分別
檢查區域邊界、最終共同有效海域與上游設定雜湊。

貢寮正式設定是
[`configs/gongliao_surface_svd_available_2024_2025.json`](configs/gongliao_surface_svd_available_2024_2025.json)，固定採用
`[121.91, 122.06, 25.00, 25.15]`，並依雙年度全部可得月份的實際 `status` 與
`cache_kind` 建立時間軸。前四區仍是 candidate，成果會標記為 `candidate_pilot`；北竿
與南竿的核定區則在完全通過資料品質檢查時標為 `analysis_ready`。

## 新增區域：設定檔從哪裡來、如何建立

`configs/*_surface_svd_*.json` 不是由 SVD 程式自動猜測座標而生成的檔案，也不是只複製舊
檔再改 bbox。它是「前處理專案定義的分析單元」加上「本次 SVD 的時間、缺值與輸出策略」
所組成的可重現分析契約。以貢寮為例，兩端欄位的來源如下：

| SVD 設定欄位 | 權威來源／用途 |
| --- | --- |
| `focus.analysis_unit_id`、`name_zh`、`approval_status`、`flow_domain_id` | 前處理專案 `ocm_svd_analysis_units_v*.json` 的同一個 `analysis_units` 項目。 |
| `bbox_lon_lat`、`anchor_lonlat` | 同一項目的 `analysis_bbox`、`anchor_lonlat`；兩端必須逐值一致。 |
| `source_analysis_units_config`、`source_analysis_units_config_sha256` | 上游分析單元 JSON 的相對路徑與檔案位元 SHA-256，用於留下版本追溯證據。 |
| `spatial_mask_policy` | 正式分析單元固定為 `analysis_bbox_cell_center`，表示 bbox 外讀取緩衝格不進入 SVD。 |
| `input`、`mask_and_missing_data`、`svd`、`parallel_execution`、`figures` | SVD 專案的資料時間窗、品質門檻、演算法、運算資源與圖面設定；須明確審查後版本化，不可默默沿用不適合新區域的門檻。 |

新增一個研究區時，請依下列順序處理：

1. **先定義上游分析單元。** 在 `OCM-Data-Preprocessing` 更新唯一的
   `ocm_svd_analysis_units_v1.json`，加入或調整已核定的 `analysis_unit_id`、名稱、
   `candidate` 或 `approved` 狀態、所屬 `flow_domain_id`、`analysis_bbox`、區內 anchor、
   幾何與 coverage 門檻。地理範圍或核定狀態改變後，所有仍引用此 v1 的下游設定都必須同步
   更新 SHA-256 與對應欄位，避免同名設定在兩端代表不同範圍。
2. **先有可用的 surface cache。** 新區域必須落在已發布的 flow domain；若沒有，就先由
   前處理專案建立相應的 schema 3 `ocm_surface/<flow_domain_id>/`。只新增 SVD JSON 不會產生
   u、v、eta、格點面積或遮罩資料。
3. **計算並鎖定上游版本。** 對新的上游 JSON 執行
   `shasum -a 256 OCM-Data-Preprocessing/configs/ocm_svd_analysis_units_v1.json`，把完整小寫
   雜湊填入新 SVD JSON 的 `source_analysis_units_config_sha256`。
4. **從相近區域複製 SVD 設定，再逐欄更新。** 新檔可命名為
   `configs/<region>_surface_svd_<years>.json`；更新 `analysis_label` 與 `purpose`，並把 `focus`
   內的 ID、名稱、狀態、flow domain、bbox、anchor、上游設定路徑與 SHA-256 全部改為第 1–3
   步的值。`minimum_static_ocean_cells` 必須至少符合新分析單元的 coverage 設計；其他缺值
   門檻、年份、模式數與 CPU 配額亦須依資料量與 SERVER 資源重新確認。
5. **先做單區驗證，再決定是否納入批次。** 使用 `ocm-svd --config` 執行新檔；本機試跑可加
   `--allow-trial --no-figures`，正式成果不得使用 `--allow-trial`。確認輸出的
   `metadata.json` 中 analysis unit、bbox、上游 SHA、共同有效格點數與品質檢查均正確。
   若要納入多區批次，再建立或更新對應的 batch JSON，使 batch 與所有單區設定使用同一份
   上游版本與 SHA，並新增 `region_configs` 項目。
6. **更新跨專案契約測試。** 目前
   `test_six_svd_configs_match_preprocessing_analysis_unit_contract` 固定檢查 v1 的六區；新增正式
   區域或更新 AOI 時，必須更新此測試的預期分析單元集合與同步欄位，讓 CI 繼續攔截兩端
   bbox、anchor、flow domain 或核定狀態不一致的情況。

新區若只是同一個主分析區內的觀測站、受體點或局地作圖位置，通常不應建立另一份獨立
五模態 SVD。應先把它列為既有主分析單元的 subordinate focus；只有研究問題、AOI 與資料
涵蓋範圍都確實不同，且有足夠共同有效海域格點與時間樣本時，才建立新的 analysis unit。

## SERVER 執行

在 SERVER 的 SVD 專案目錄執行。完整 native cache 保留在 SERVER，不必下載到本機；
只需要同步程式碼，運算完成後再下載不可變的成果目錄。本專案以 `.python-version`
固定 Python 3.12.13，並以 `uv.lock` 固定套件解析結果。

以下設定會把 uv 管理的 Python、虛擬環境及套件快取放在使用者可寫的獨立目錄。即使
shell 提示字仍顯示 `(base)`，`UV_PROJECT_ENVIRONMENT`、`UV_MANAGED_PYTHON=1` 及命令列
的 `--python 3.12.13` 仍會使本專案避開 `/home/mustlab/anaconda3/bin/python`；不需要
修改或移除 SERVER 原有 Anaconda。

```bash
cd /home/mustlab/Workspace/OCM-SVD-Analysis

export UV_CACHE_DIR=/home/mustlab/work/uv-cache/ocm-svd-analysis
export UV_PROJECT_ENVIRONMENT=/home/mustlab/work/venvs/ocm-svd-analysis-py312
export UV_MANAGED_PYTHON=1
export MPLCONFIGDIR=/home/mustlab/work/matplotlib-cache/ocm-svd-analysis
export PYTHONDONTWRITEBYTECODE=1

uv python install 3.12.13
uv sync --frozen --python 3.12.13 --managed-python

export OCM_SURFACE_ROOT=/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/ocm_surface
export SVD_OUTPUT_ROOT=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results

uv run --frozen --no-sync --python 3.12.13 ocm-svd-batch \
  --batch-config configs/six_regions_surface_svd_available_2024_2025_batch.json \
  --surface-root "$OCM_SURFACE_ROOT" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --no-figures
```

正式雙年度批次會依六個分析單元分別發布不可覆寫的 run 目錄；若需正式圖面，完成數值
成果驗收後再使用既有 run 進行唯讀重繪，不重新讀取原始 cache 或重跑 SVD。

```text
$SVD_OUTPUT_ROOT/svd/<analysis-label-2024_2025>/
```

### 後灣完整 flow-domain 水柱直接 SVD（本次正式 SERVER 作業）

完整兩年 paired cache 僅留在 SERVER；本機只執行合成資料與一日 trial，不能取代正式 run。
同步本 repository（含 `uv.lock`）後，先設定 native root，並執行唯一的後灣入口：

```bash
export OCM_NATIVE_ROOT=/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/ocm_native
export OCM_SURFACE_ROOT=/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/ocm_surface
export SVD_OUTPUT_ROOT=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-06

uv sync --frozen --python 3.12.13 --managed-python
./scripts/run_houwan_flow_domain_water_column_svd_available_2024_2025.sh
```

腳本先執行唯讀 paired preflight，將完整 grid、24 個月 UTC 軸、候選矩陣、可用磁碟與預定
direct solver 寫入 `logs/*.json`；通過後才建立暫存矩陣並正式求解。正式成果發布為：

若正式 run 在「加權矩陣已完成」之後因 PROPACK、殘差驗證、序列化或圖面問題失敗，程式會把
`weighted_anomaly_float64.dat`、`solver_resume_checkpoint.npz` 與
`solver_failure_diagnostic.json` 原子保留於同一 namespace 的隱藏 recovery 目錄。這些是未發布
工作檔，不能直接當成成果或手動修改；它們會綁定 JSON 設定 hash、完整 canonical UTC 軸與候選
feature 數，並保存已選欄位布局及完整 candidate feature 有效率 QC 軸。確認失敗日誌的 recovery
路徑後，可只重試相同的直接 SVD：

```bash
export SVD_RESUME_PARTIAL="$SVD_OUTPUT_ROOT/water_column_svd/.houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1.recovery-<uuid>"
./scripts/run_houwan_flow_domain_water_column_svd_available_2024_2025.sh
```

續跑仍會重新檢查 paired cache 與 UTC 軸，但不會再讀取兩年 native 3D 資料或改變 mask、權重、
深度層與 eta 的單一 2D 欄位。若 checkpoint 驗證不通過，必須重新建立矩陣，不能為了省時混用
不同資料版本。失敗的 CLI stdout/stderr 會保留為 `logs/*.failed.log`，不再因 tmux 結束而遺失
traceback；成功發布時 recovery scratch、checkpoint 與失敗診斷均會移除。

```text
$SVD_OUTPUT_ROOT/water_column_svd/
└── houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1/
    ├── mode_u_mps_per_raw_pc.npy        # (mode, velocity_level, lat, lon)
    ├── mode_v_mps_per_raw_pc.npy        # (mode, velocity_level, lat, lon)
    ├── mode_eta_m_per_raw_pc.npy        # (mode, lat, lon)，沒有 depth 軸
    ├── pc.npy                            # (mode, time)，20 條共同 PC
    ├── metadata.json                     # solver、殘差、遮罩、時間與資源證據
    └── figures/
        ├── report/water_column_mode_01_surface_spatial_report.png/.svg
        ├── report/water_column_mode_01_z_minus_010m_spatial_report.png/.svg
        ├── report/water_column_mode_01_eta_spatial_report.png/.svg
        ├── report/water_column_mode_01_pc_report.png/.svg
        ├── report/water_column_svd_explained_variance_report.png/.svg
        ├── report/water_column_*_feature_coverage_qc_report.png/.svg
        ├── REPORT_GUIDE.md
        └── plot_metadata.json
```

每一張速度空間**主圖本身**都在右下角以表層圖同款的箭頭、數值與單位內嵌同一 q95 向量參考尺；不再要求使用者從
`_with_vector_scale` 備用版本選圖。每張圖仍另附同 stem 的 `_vector_scale_transparent`
透明素材，僅在報告版面必須將比例尺移至圖外時使用。雙行標題與固定色條欄位分離，避免
長中文模態題名遮擋右側色條。各圖的色階、箭頭尺度、有效格點數、同 mode 的 PC 配對關係
與文獻圖面慣例均保存於 `figures/plot_metadata.json`；因此報告可單獨取用任一深度圖，不需
先裁切複合圖。

水柱圖面採「數值上聯合、圖面上拆分」：六層速度、唯一 eta 與共同 PC 仍是同一次
聯合 SVD 的結果，但不再產生 `mode_XX.png`、2×4 coverage 或 3×3 複合畫布。每個模態
的六層速度圖、eta 圖與 PC 圖各自為可獨立引用的正式圖檔；解釋變異圖與七張深度／eta
coverage QC 圖也各自輸出。此組織方式參考 EOF/PC 空間型態與時間係數的配對概念，以及
三維海洋研究中水平與垂向視角分開檢視的做法；完整來源與引用界線見
[`docs/svd_figure_reference_log.md`](docs/svd_figure_reference_log.md)，其中包含
[Lee et al. (2013) Ocean Dynamics](https://link.springer.com/article/10.1007/s10236-013-0643-z)
與本次提供的其他文獻。`plot_metadata.json` 會記錄 `logical_figure_count=168`、圖面
schema、同 mode 配對、eta 不重複進入垂向層的限制，以及所有衍生比例尺資產。

#### 已有完整水柱 SVD 結果：只讀陣列重繪獨立圖面

若既有 `water_column_svd/<analysis_label>/` 已保存 `regression_u_mps_per_pc_std.npy`、
`regression_v_mps_per_pc_std.npy`、`regression_eta_m_per_pc_std.npy`、
`pc_standardized.npy`、`explained_variance.npy`、兩種 feature mask，以及 `lon/lat/time`
座標，圖面修正**不可**再次執行 `run_houwan_flow_domain_water_column_svd_available_2024_2025.sh`。
應以 `ocm-svd-water-column-replot` 建立另一個 immutable figure bundle；此入口沒有
native/surface root 參數，僅以 `numpy.memmap` 唯讀既有水柱 SVD 陣列，且不會重新建立加權
矩陣、垂向內插、正規化、固定模態符號或呼叫直接 SVD solver。

SERVER 上的舊版成果是 20 模態、時間×feature 方向的歷史 run；它保留作為相容性與回溯
對照，不代表目前正式設定。現行正式水柱成果採研究需求指定排列
`A=(feature,time)`，求取 100 個模態且預設繪製 20 個；其完整陣列包含 17,052 個共同 UTC
時次、`(100, 6, 102, 152)` 的 u/v 模態場與 `(100, 102, 152)` 的唯一 eta 模態場。
後續可對同一成果唯讀重繪第 21–30 或其它已計算模態：

```bash
cd /home/mustlab/Workspace/OCM-SVD-Analysis
uv sync --frozen --python 3.12.13 --managed-python

SOURCE_RUN=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-07_water_column_host100/water_column_svd/houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1
BUNDLE_ROOT=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-07_water_column_figure_refresh

uv run --frozen --no-sync --python 3.12.13 ocm-svd-water-column-replot \
  --run-dir "$SOURCE_RUN" \
  --output-root "$BUNDLE_ROOT" \
  --config configs/houwan_nmmba_flow_domain_water_column_svd_available_2024_2025.json
```

成功後會發布至
`$BUNDLE_ROOT/water_column_svd_figure_bundles/houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1/academic_report_ready_water_column_independent_v1/`。
此路徑與既有科學 run 分離，來源的 `.npy`、`metadata.json`、`config.json`、舊圖與上游
cache 均不會被覆寫；程式在繪圖前後都雜湊來源檔，若有任何來源改變即拒絕發布。輸出包含
168 個邏輯獨立圖面（預設 20 個模態各六張速度圖、一張 eta 圖、一張 PC 圖，外加一張 scree 與
七張 coverage QC 圖），以 PNG 與 SVG 提供，並另附向量比例尺衍生資產。已存在同一 bundle
版本時，程式會拒絕覆寫，請改用新的 `BUNDLE_ROOT` 或在圖面規格確實變更後提升 style 版本。

若 `metadata.json` 顯示 `direct_propack_streaming`，它仍是針對同一加權矩陣的直接 SVD；
`direct_dense_lapack` 與此策略只有記憶體存取方式不同。兩者都必須有設定要求的模式數、有限正奇異
值、通過殘差／正交性檢查，並且 `mode_eta_*` 維度只能是 `(mode, lat, lon)`。不要在正式命令
加 `--allow-trial` 或 `--no-figures`。

雙年度資料中的共用東北臺灣快取，其 `202507` 前 24 筆時間座標採用預先登錄的 +24 小時時間軸
正規化假設：該段原始快取標籤為 `2025-06-30T01:00Z` 至 `2025-07-01T00:00Z`，而相鄰日檔
命名與後續時序顯示跨月銜接存在不一致。這是專案端為維持分析時間軸內部一致性所作的處置，
並非原始 NetCDF 資料提供者的更正或確認。`input.known_time_axis_repairs` 是既有設定欄位名稱；
它只在原始起訖時間完全相符時，於分析記憶體內將前 24 筆時間標籤平移 24 小時。此處置不覆寫
上游 `.npy`、不重排樣本，也不改變 u/v/eta/valid 數值；假設內容與套用筆數會寫入成果 metadata。

目前 repository 只維護表層 SVD 與完整 flow-domain 水柱直接 SVD；舊的垂向比較 family 已自
原始碼、設定、CLI、測試與成果目錄移除，不再作為可重現產品。

## 平行化執行

單區設定已啟用兩段不重疊的平行化：

- `parallel_execution.io_workers=4`：最多四個 worker 同時以 memory-map 讀取不同月份的
  focus bbox 小窗；主流程仍依 2024-01 至 2025-12 排序串接，因此平行完成順序不會改變
  SVD 時間軸或數值結果。
- `parallel_execution.linear_algebra_threads=4`：I/O worker 結束後，透過 `threadpoolctl`
  限定 NumPy 背後的 BLAS，以最多四個 CPU 核心計算協方差、特徵分解、PC 與重建檢查。

這個「先 I/O、後線性代數」的安排避免巢狀平行化讓 4 個讀檔 worker 與多核心 BLAS 同時
超額使用 SERVER CPU。實際使用數會自動限制到 SERVER 可見 CPU 數量，並寫入每次成果的
`metadata.json > parallel_execution`。若 SERVER 的共享儲存體因併發讀取變慢，可將
`io_workers` 降為 2；若排程系統只配置 2 核，應把 `linear_algebra_threads` 設為 2。

### 效能計時與瓶頸定位

每個新科學 run 的 `metadata.json > performance` 使用
`time.perf_counter()` 單調 wall clock，記錄下列互不重疊階段：

| metadata 階段 | 量測內容 |
| --- | --- |
| `configuration_and_output_validation` | JSON 解析、科學設定驗證與不可覆寫輸出檢查。 |
| `surface_focus_month_io` | 各月份 memory-map 開啟、bbox 小窗複製及月序串接。 |
| `mask_and_missing_data_preparation` | cell-center、靜態海域、三變數共同有效率與短缺值處理。 |
| `svd_solver` | 正規化、面積加權 covariance、`eigh`、PC 投影及重建檢查。 |
| `physical_and_visualization_field_derivation` | 物理 loading、標準化 PC、回歸模態與平均場回填。 |
| `array_and_provenance_serialization` | `.npy`、`config.json` 與空間切片 sidecar 寫入。 |
| `figure_rendering` | PNG、SVG 與 `plot_metadata.json` 產生；使用 `--no-figures` 時仍明載近零耗時。 |

`performance.total_seconds` 的範圍截至 metadata 內容組裝，不含最後一次
`metadata.json` 寫入及原子目錄 rename，因為 immutable 成果不能在發布後回寫自身。
六區 batch JSON 的每區 `elapsed_seconds` 會包含這兩步，兩者應搭配解讀。wall time 包含
I/O 等待與多執行緒 BLAS 的實際經過時間，不等於所有 CPU time 的總和。

### 既有 SVD 成果重繪：不重新求解

只修改圖面時，不再重讀 surface cache 或重跑 SVD。單區重繪命令如下：

```bash
uv run ocm-svd-replot \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<既有-run-id>" \
  --output-root "$SVD_OUTPUT_ROOT"
```

重繪器以唯讀 memory-map 使用既有的 `mean_*`、`regression_*`、
`pc_standardized.npy`、`time_utc_ns.npy` 與 `explained_variance.npy`，並發布到：

```text
$SVD_OUTPUT_ROOT/
└── svd_figure_bundles/
    └── <來源-run-id>/
        └── <figure-style-version>/
            ├── figure_config.json
            ├── metadata.json
            └── figures/
```

對外目錄與 `bundle_id` 只保留可讀的 figure style 版本，例如
`academic_report_ready_v8`，不再附加 hash。來源 metadata、`figures` 設定、renderer
原始碼、繪圖環境與字型仍共同形成完整 `bundle_provenance_sha256`，保存在 bundle
`metadata.json`。同一版本一旦發布便拒絕覆寫；繪圖程式或視覺規格若要改變，必須先把
`figures.style` 升版，例如由 v7 升為 v8，不能在同一版本下並存多個 hash 目錄。
來源科學 run 不新增 `figures/`、不改 metadata，也不複製科學陣列。若要改 DPI、輸出
格式或 mode count，可另外提供一份已升版的完整設定：

本次圖面文字契約升版為 `academic_report_ready_v9`：沿用 v8 的版面與月份刻度，更新
海域名稱、奇異值分解／SVD、主成分時間係數／PC、coverage 說明與圖例文字。v9 是新的
不可覆寫 figure bundle 版本，舊 v6/v7/v8 圖包仍保留供追溯與比較。

```bash
uv run ocm-svd-replot \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<既有-run-id>" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --config configs/<只修改-figures-的完整設定>.json
```

重繪器會雜湊比較除 `figures` 外的所有設定；年份、bbox、缺值規則、SVD 正規化或模式數若
有任何不同就停止，避免替既有陣列貼錯標籤。

六區報告圖可一次平行產生，預設最多六個獨立 process：

```bash
uv run ocm-svd-replot-batch \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<龜山島-run-id>" \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<貢寮-run-id>" \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<新竹-run-id>" \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<後灣-run-id>" \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<北竿-run-id>" \
  --run-dir "$SVD_OUTPUT_ROOT/svd/<南竿-run-id>" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --max-concurrent-regions 6
```

如六區都要提供修改過的完整設定，依 `--run-dir` 相同順序重複六次 `--config`。批次輸出
JSON 保持輸入區域順序，並記錄每區與總耗時，報告組裝程式不必依 worker 完成順序猜測版面。
建議 2024–2025 正式批次先用 `ocm-svd-batch --no-figures` 完成六個 immutable 科學
run，再以此命令產生報告圖；後續修改 renderer 時只重繪 bundles。

### 2024–2025 正式設定

讀取器已支援以 `input.years` 依「年份、月份」順序平行讀取 24 個完整月檔。本 repository
已提供六份雙年度正式設定，以及其受控平行 batch；各設定與成果均以雙年度版本獨立管理：

- `configs/guishan_surface_svd_2024_2025.json`
- `configs/gongliao_surface_svd_2024_2025.json`
- `configs/hsinchu_surface_svd_2024_2025.json`
- `configs/houwan_nmmba_surface_svd_2024_2025.json`
- `configs/beigan_surface_svd_2024_2025.json`
- `configs/nangan_surface_svd_2024_2025.json`
- `configs/six_regions_surface_svd_2024_2025_batch.json`

每份單區設定的 input 段為：

```json
"input": {
  "years": [2024, 2025],
  "months": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  "required_cache_schema_major": 3,
  "required_status": "ready",
  "required_cache_kinds": ["standard_month"],
  "expected_timestep_hours": 1.0,
  "maximum_source_gap_hours": 2.0
}
```

各 `analysis_label` 與 batch `batch_label` 都已包含 `2024_2025`。輸出會保存 24 個來源月份
metadata hash，且測試已驗證讀取順序為 2024-01…2024-12、2025-01…2025-12。以至少 32 核
SERVER 執行數值成果（暫不繪圖）時，使用：

```bash
uv run --frozen --no-sync --python 3.12.13 ocm-svd-batch \
  --batch-config configs/six_regions_surface_svd_2024_2025_batch.json \
  --surface-root "$OCM_SURFACE_ROOT" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --no-figures
```

若某月份是 `standard_partial_month`，預設會拒絕執行。只有研究團隊已決定接受缺日時才加
`--allow-partial-months`；程式仍會拒絕超過設定值（預設 2 小時）的實際時間缺口。`--allow-trial`
只用於本機單日 smoke test，輸出會標示 `trial_pilot`。

### 2024–2025 全部可得資料設定

當研究團隊明確決定接受無法補齊的來源缺日，必須使用獨立的 `available_2024_2025` 科學
契約，不能把嚴格完整月設定加上 `--allow-partial-months` 後直接混用。本版提供六份
`*_surface_svd_available_2024_2025.json` 與
[`configs/six_regions_surface_svd_available_2024_2025_batch.json`](./configs/six_regions_surface_svd_available_2024_2025_batch.json)。

這組設定保留上游 `ready` 月份中的 `standard_month` 與經 CLI 明確授權的
`standard_partial_month`，不跨來源斷點插補，並以 `maximum_source_gap_hours: null` 明確解除
來源缺口長度上限。對原始時序已不可考的跨日／跨夜 UTC 倒序或重複，這組設定明確採
`time_axis_canonicalization_policy: sort_and_deduplicate_prefer_last`：先以 UTC 穩定排序，再
對每個相同 UTC 保留設定年份、月份與月內索引序列中最後出現的一筆樣本。它不補值、不修改
u/v/eta 數值；重排與去重筆數、實際最大缺口、斷點數及覆蓋率皆寫入
`metadata.json > input_surface.time_axis_canonicalization` 與 `source_time_axis`。canonicalization
後仍要求唯一 UTC 軸的中位採樣步長符合 1 小時；若不符，必須以 metadata 與研究限制另行說明。
龜山島與貢寮的共用東北臺灣 cache 另明載 `202507` 前 24 筆 UTC 時間座標的預先登錄
正規化假設；報告須揭露其為專案端處置，而非資料提供者確認。

正式 SVD 前應先執行唯讀預檢器；它只讀 24 個月的 `metadata.json` 與
`time_utc_ns.npy`，逐區列出 partial 月份、最大來源缺口、斷點數及 canonicalization 的重排／
去重筆數，不建立 output 目錄：

```bash
./scripts/preflight_surface_svd_time_axis.sh
```

所有專案 bash 腳本都會在 `<project-root>/logs/` 自動建立一份 UTF-8 JSON 執行日誌，
檔名包含腳本名稱、UTC 啟動時間與 PID。日誌保存命令列參數、實際採用的輸入根目錄、
開始／結束時間、exit code 與逐區事件；預檢日誌另含每區 partial month、時間缺口、
canonicalization 統計與錯誤摘要。`logs/` 屬作業證據，已排除於 Git 版本控制。

預檢六區均為 `OK` 後，再由正式 bash 入口啟動 batch：

```bash
./scripts/run_surface_svd_batch_available_2024_2025.sh
```

這個入口會直接產生完整 science run 與其正式圖面；若只需先完成數值成果、延後產圖，仍
可使用 Python CLI 的 `--no-figures`，但該直接 CLI 呼叫不會自動建立 bash JSON log。

### 北竿／南竿 AOI 更新後重跑

北竿與南竿 AOI 直接依前處理專案
[`ocm_svd_analysis_units_v1.json`](/Users/mustlab/Workspace/OCM-Data-Preprocessing/configs/ocm_svd_analysis_units_v1.json)
更新；北竿範圍為 `[119.93, 120.04, 26.18, 26.30]`，南竿範圍為
`[119.88, 120.00, 26.10, 26.19]`；兩者
在重疊帶各自獨立納入有效 cell center。兩區皆完整位於既有
`lienchiang_common_cache_v3`，所以只要 2024–2025 月份快取仍為 `ready`，不需要重跑
前處理。

先將兩個 repository 的更新檔案同步至 SERVER，並在 SVD 專案執行只讀預檢：

```bash
./scripts/preflight_surface_svd_time_axis.sh \
  configs/lienchiang_surface_svd_available_2024_2025_batch.json \
  "$OCM_SURFACE_ROOT"
```

兩區均顯示 `OK` 後，在 tmux 內直接執行正式入口：

```bash
./scripts/run_lienchiang_surface_svd_available_2024_2025.sh
```

該入口預設同時求解北竿與南竿、接受已核定的 `standard_partial_month`、保留完整 v8 報告
圖於各自 `svd/<run-id>/figures/`，並在 `logs/` 分別留下預檢與 batch JSON 日誌。若要建立
成果報告使用的獨立 v8 figure bundle，必須再執行原本的六區重繪入口：

```bash
./scripts/replot_available_surface_svd_v8.sh "$SVD_OUTPUT_ROOT"
```

此單一六區重繪入口會比較排除 `figures` 後的完整設定內容，精確選取北竿／南竿更新後 AOI
對應的 science run；舊 AOI run 不會被選取。其餘四區仍使用唯一的既有 science run，且只
以目前 v8 figures 規格重繪，不修改其歷史科學設定。圖包發布於
`$SVD_OUTPUT_ROOT/svd_figure_bundles/<新-run-id>/academic_report_ready_v8/`，且不讀取
surface cache 或重新求解 SVD。若 SERVER 中斷後只有其中一區已原子發布，
才明確使用下列方式續跑；它只重用設定雜湊相同的已完成 run，絕不覆寫更新前成果：

```bash
SVD_SKIP_EXISTING=1 ./scripts/run_lienchiang_surface_svd_available_2024_2025.sh
```

成果會建立在 `$SVD_OUTPUT_ROOT/svd/` 下的
`beigan_surface_u_v_eta_available_2024_2025_v1_<config-hash>` 與
`nangan_surface_u_v_eta_available_2024_2025_v1_<config-hash>`；更新前目錄雖使用相同 label，
但其 config hash 不同，仍是舊 AOI 的獨立成果，不能與更新後圖表或統計混用。

```bash
uv run --frozen --no-sync --python 3.12.13 ocm-svd-batch \
  --batch-config configs/six_regions_surface_svd_available_2024_2025_batch.json \
  --surface-root "$OCM_SURFACE_ROOT" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --allow-partial-months \
  --no-figures
```

科學 run 完成後，所有報告、圖說與 metadata 解讀都必須以「2024–2025 全部可得樣本」描述，
並引用每區 `metadata.json > input_surface.source_months`、`time_window` 與
`parallel_execution` 所記錄的實際來源、覆蓋率與運算條件；還必須揭露
`input_surface.time_axis_canonicalization`。不得稱為無缺口的 2024–2025 兩年資料。

### 已完成科學 run 的 v8 可讀性重繪

既有 `academic_report_ready_v6` 圖若出現跨兩年 PC 的逐月 `YYYY-MM` 標籤重疊，不可
重跑或覆寫 SVD 科學成果。v8 保留全部逐月 `YYYY-MM`，以 270° 直式橫寫並增加 PC 圖
下方高度；地圖的經緯度刻度由六個縮為五個，並調低座標／色條字級。平均場、模態圖、PC
圖與解釋變異圖均共用此版面契約。
在 SERVER 同步本 repository 的 v8 程式與設定後，執行：

```bash
./scripts/replot_available_surface_svd_v8.sh "$SVD_OUTPUT_ROOT"
```

重繪腳本同樣在 `logs/` 產生 JSON 日誌，標示各區為 `queued`、`skipped`、`pending` 或
`error`，可在 tmux 斷線或批次重跑後確認實際處理狀態。

腳本只重繪已存在的六區 scientific run，並平行建立
`$SVD_OUTPUT_ROOT/svd_figure_bundles/<run-id>/academic_report_ready_v8/`。它不讀取
`$OCM_SURFACE_ROOT`、不改寫既有 `svd/<run-id>`、不重新求解 SVD；尚未完成的區域標為
`PENDING`，可待科學 run 發布後以相同指令再次執行。

## 既有表層產品的 SVD 方法（不適用於本次水柱直接 SVD）

### 既有表層矩陣建立順序：陸地與 NaN 不可進入矩陣

![OCM 海流 SVD 遮罩篩選與矩陣轉換概念圖](docs/figures/ocm_svd_mask_matrix_concept_v1.png)

圖中左側是 flow-domain 的分析讀取小窗：海域有效格點可保留，陸地及其 NaN 值、AOI 外與
讀取緩衝格都不進入分析；中間列出共同有效遮罩條件；右側則顯示只由保留海域格點組成的
`X_w (3P × N)` SVD 矩陣與其空間模態、時間係數輸出。

下列順序完整定義海域格點保留、陸地格點排除與缺值處理規則。程式先以完整讀取小窗建立
遮罩，再只抽取最終共同有效的海域 cell；不會把陸地 NaN 或未處理的資料缺值交給 SVD
線性代數：

1. **分析邊界：** `analysis_geometry_mask.npy` 只保留版本化 AOI bbox 內的 cell center；
   為 memory-map 安全而向外多讀的一格只屬於讀取緩衝，不可成為矩陣欄位。
2. **靜態陸海遮罩：** 只保留 `mask_static.npy=True` 的海域 cell。陸地（包括其原始
   `u/v/eta=NaN`）在這一步已不具進入資格。
3. **逐時三變數有效性：** 對每個海域 cell、每個時間步，必須同時為
   `valid_mask_surface.npy=True`、`u_surface_mps` 有限、`v_surface_mps` 有限與 `eta_m`
   有限；任一條件不成立即視為該 cell 的三變數共同缺值。
4. **固定空間欄位：** 僅保留全分析期間三變數聯合有效率至少 95% 的 cell，形成
   `valid_mask.npy`。程式以 `np.where(valid_mask)` 取出這些 `P` 個 cell，建立
   `(time, component[u,v,eta], P)` 資料陣列；被排除的陸地與無效格點沒有對應的 `P` 欄位。
5. **時間樣本完整性：** 只對同一 cell 前後皆有效、連續不超過 2 個時間步的短缺值線性
   插補。插補後若任一保留 cell 還有 NaN，整個時間步會移除。故真正送入 SVD 的
   加權矩陣完全不含 NaN，也絕不以 0 補值。

這個規則適用於 2024–2025 雙年度正式分析；有效率統計期間依各設定所列的全部年月計算。

對保留的 `P` 個海域格點與 `N` 個完整時次，狀態向量為：

$$
x(t) = [u'_1/U_0,\ldots,u'_P/U_0,
        v'_1/U_0,\ldots,v'_P/U_0,
        \eta'_1/E_0,\ldots,\eta'_P/E_0]^T
$$

其中 $u'$、$v'$、$\eta'$ 是各格點的時間距平；$U_0$ 是 u/v 共用的面積加權
向量均方根（root mean square, RMS），$E_0$ 是 eta 的面積加權均方根。RMS 是距平振幅
的代表尺度，因此可讓單位不同的流速與 eta 在同一個 SVD 中具有可比較的影響力：

$$
U_0 = \sqrt{\frac{\sum_{t=1}^{N}\sum_{p=1}^{P}a_p[(u'_{t,p})^2+(v'_{t,p})^2]}
                         {2N\sum_{p=1}^{P}a_p}},
\qquad
E_0 = \sqrt{\frac{\sum_{t=1}^{N}\sum_{p=1}^{P}a_p(\eta'_{t,p})^2}
                         {N\sum_{p=1}^{P}a_p}}
$$

其中 $a_p$ 是第 $p$ 個格點的面積。RMS 使用 $N$ 作為標準化尺度；後續協方差則使用
$N-1$ 作為樣本協方差分母。這避免 m/s 的流速與 m 的海面高度直接串接時，因量綱或數值
大小不同而由單一變數支配結果。

再對每個變數各重複一次 $\sqrt{a_p}$ 面積權重，形成加權矩陣 $X_w$。
SVD 的空間模態、奇異值與時間係數遵守：

$$
X_w = U\Sigma V^T,\qquad PC=\Sigma V^T
$$

### 既有表層程式實際怎麼做：由協方差回復 SVD

求解位置是
[`solve_surface_multivariate_svd()`](/Users/mustlab/Workspace/OCM-SVD-Analysis/src/ocm_svd_analysis/surface_multivariate_svd.py)
中的 `np.linalg.eigh(covariance)`，不是名稱看起來最直觀的 `np.linalg.svd(...)`。這不是
改用別種分析，而是利用本案「空間狀態數 `3P` 小於完整時次數 `N`」的條件，以較省記憶體
的等價解法求同一個 SVD：

$$
C = \frac{X_wX_w^T}{N-1} = U\Lambda U^T,
\qquad
\lambda_k=\frac{\sigma_k^2}{N-1},
\qquad
\sigma_k=\sqrt{\lambda_k(N-1)}
$$

程式先用 `np.linalg.eigh(C)` 取得 `Lambda`（`eigenvalues`）與 `U`
（`spatial_vectors`），再依特徵值由大到小排序。前述關係式將特徵值轉為
`singular_values`；接著以

$$
PC = U^T X_w = \Sigma V^T
$$

回復每個模態的時間係數。故 `pc.npy` 的每列對應一個空間模態，
`mode_u.npy`、`mode_v.npy`、`mode_eta.npy` 則是把 `U` 先除回面積權重、再乘回各變數
RMS 後得到的物理單位 loading。解釋變異率是
`explained_variance_k = lambda_k / sum(lambda)`，等價於
`sigma_k^2 / sum(sigma^2)`。

報告可用的精簡說法是：「我們先對 u、v、eta 去除時間平均，依各自尺度標準化並作格點
面積加權，將三個分量串成 `3P × N` 矩陣。程式對其空間協方差矩陣做特徵分解；依
`sigma_k^2=(N-1)lambda_k` 回復 SVD 奇異值，再投影回復 PC。因此求得的空間模態、
PC 與解釋變異與薄型 SVD 完全等價。」

表層雙年度設定預設計算前 20 模態，且要求至少可報告前 5 模態；正式報告仍應依資料品質、
低通／季節敏感度、North rule 與 block bootstrap 等分析結果決定實際解讀範圍。

第 5 步的所有插補位置都會寫入 `imputed_mask.npy`，供 SVD 成果審查與不插補敏感度
分析使用。

## 輸出內容

每個 run 都含下列可供 review 與後續敏感度分析的檔案：

| 檔案 | 維度 | 意義 |
|---|---|---|
| `mode_u.npy`、`mode_v.npy`、`mode_eta.npy` | `(mode, lat, lon)` | 回復到物理分量尺度的 SVD loading；非共同海域格點為 NaN。 |
| `pc.npy` | `(mode, time)` | 求解器原始的加權 SVD 時間係數。 |
| `pc_standardized.npy` | `(mode, time)` | 每一模態移除浮點均值尾差並以樣本標準差（ddof=1）正規化的無因次 PC。 |
| `regression_u.npy`、`regression_v.npy`、`regression_eta.npy` | `(mode, lat, lon)` | PC 增加 1 個標準差時，各物理分量對應的回歸空間模態；供學術圖與直接物理解讀。 |
| `singular_values.npy`、`explained_variance.npy` | `(mode,)` | 奇異值與各主要模態對全部變異的解釋比例。 |
| `mean_u.npy`、`mean_v.npy`、`mean_eta.npy` | `(lat, lon)` | SVD 前的全年時間平均；不可與距平 loading 混解讀。 |
| `analysis_geometry_mask.npy` | `(lat, lon)` | 版本化 AOI bbox 內的 cell center；不含靜態或逐時有效性篩選。 |
| `valid_mask.npy` | `(lat, lon)` | analysis geometry、靜態海域與三變數共同有效率交集。 |
| `cell_triplet_valid_fraction.npy` | `(lat, lon)` | u/v/eta 聯合有效率，供檢查排除原因。 |
| `imputed_mask.npy` | `(time, component, lat, lon)` | 短缺值插補位置；component 順序是 u、v、eta。 |
| `metadata.json` | JSON | 設定、月份 metadata hash、遮罩、權重、RMS、正負號、數值檢查、逐階段效能與限制。 |
| `figures/` | PNG、SVG、JSON、Markdown | `report/*_report` 白底完整標示圖、圖面 sidecar 與 bundle 內報告指南。 |

圖中的模態箭頭是 SVD loading，不是特定時刻的實際流速；需與同一模態 PC 相乘後才是
對距平流場的重建。每次 run 都以設定與上游月份 metadata hash 形成 ID，既有成果拒絕覆寫。

### 學術圖表：正式成果只交付可自我解釋的報告版

2024–2025 全部可得資料設定採 `academic_report_ready_v8`，依海洋流場 SVD 論文的共同表達方式，只交付
能在脫離程式碼與 sidecar 後仍可讀懂的完整報告圖：

- `figures/report/*_report.{png,svg}` 是不透明白底的正式報告版，內含區域與模態標題、
  完整中文解釋變異量、經緯度與單位、η 色條、高解析海岸線、PC 圖例與月份刻度。跨兩年
  PC 固定保留全部逐月 `YYYY-MM`，並以 270° 直式橫寫避免標籤重疊；地圖經緯度軸為五個
  含端點刻度。
  經緯度軸與色條都強制顯示實際上下限；SVD 正負號 anchor 不畫在圖上，避免被誤認
  為測站。
- 每張平均場／模態空間圖另有同 stem 的
  `*_vector_scale_transparent.{png,svg}`，只顯示該圖實際 q95 向量與單位。其背景
  alpha 完全透明，內容是純黑箭頭與數值，不含白色底板、半透明框或 halo；PowerPoint
  後製建議使用 SVG。素材已依箭頭與文字的實際外框對稱裁切，參考箭頭也依主圖 q95
  箭頭的顯示長度產生；主圖與 SVG 以原始尺寸匯入後應先群組再一起縮放，放在右下角
  並內縮約 2.5%，不要再單獨放大參考尺。
- 每張空間圖另產出 `*_with_vector_scale.{png,svg}` 備用完整圖，已用主圖相同 quiver
  scale 把參考箭頭畫在座標框內右下角。版面沿用 `OCM-NetCDF-Visualizer` 與
  `OCM-Data-Preprocessing` 的 axes-fraction quiverkey、東側標籤及半透明矩形結構，
  但縮成 26.0%×6.0% 的緊湊框。這個版本適合不想手動疊圖時直接使用；標準
  `*_report` 主圖仍不含參考尺，兩者不能同時疊用。
- `figures/REPORT_GUIDE.md` 會隨每個 immutable figure bundle 產生，記錄該次實際樣本
  覆蓋、斷點、前五模態單一／累積解釋變異量、色階與箭頭 q95 尺度，以及空間圖、
  配對比例尺與 PC 的解讀規則；圖檔搬離專案後仍保有最小必要說明。

兩套圖共同遵守下列科學語意：

- 空間模態以 `regression_eta.npy` 作零中心紅藍對稱底色，疊加 `regression_u.npy` 與
  `regression_v.npy` 流速箭頭；數值代表對應 PC 改變 1 個標準差時的物理量變化。若整張
  η 圖只有藍色，表示採目前正負號慣例時該模態 η 回歸值全為負，不是繪圖錯誤。
- 每一個空間模態各有一張獨立的標準化 PC 時序；淡灰細線保留逐時值，黑線是只供全年
  視覺閱讀的日平均。兩層在缺日時段都會斷線，不以直線跨越缺測。
- 解釋變異量另以單一模態長條與累積折線輸出。
- `figures/plot_metadata.json` 保存 bbox、單位、report-only 交付政策、每張圖的色階、
  箭頭尺度、解釋變異量、明確邊界刻度、缺口數、實際 CJK 字型與文獻依據。
- 空間圖裁到 analysis bbox 與最外側有效 cell edge 的交集，不把無資料的 bbox 尾端畫成
  白帶；距邊界過近而可能被裁切的箭頭不畫，但完整底色與數值陣列不受影響。
- 空間圖以 `OCM-Data-Preprocessing/data/coastline/osm_land_polygons_taiwan_v1.geojson`
  疊加 OSMData／OpenStreetMap 高解析向量陸地；暖灰陸地與深灰海岸線只作地理參照，
  不改變 1 km OCM 流場、分析遮罩、SVD 權重或統計。來源 SHA-256 與 ODbL attribution
  會寫入每個 figure bundle。
- 產圖設定接受 `academic_report_ready_v6`／`academic_report_ready_v7`（既有圖包重現）
  與目前正式版 `academic_report_ready_v8`；程式不保留無文字主圖或透明主圖相容
  分支，避免六區批次重新產出「拿到檔案仍不知道代表什麼」的交付物。唯一透明資產是
  檔名明示 `_vector_scale_transparent` 的後製參考尺，內容仍保留數值與單位。

### 報告時如何解讀空間圖與 PC

每個模態必須把 `svd_mode_XX_spatial_report`、同 stem 的
`svd_mode_XX_spatial_report_vector_scale_transparent` 與
`svd_mode_XX_pc_report` 成對呈現；若不手動後製，使用
`svd_mode_XX_spatial_report_with_vector_scale` 取代前兩者。
空間圖不是某個日期的實際流況，而是「標準化 PC 增加 1σ 時」的共同距平形態：

1. PC 為正時，η 與 u/v 依空間圖的色號及箭頭方向解讀。
2. PC 為負時，η 正負與全部箭頭方向反轉。
3. 單一模態在某時刻的距平貢獻等於空間回歸圖樣乘上該時刻標準化 PC。
4. SVD 的整組空間模態與 PC 同時乘以 -1 仍是同一解，因此不可脫離 PC 單獨把
   「藍色」命名為下降事件。
5. 解釋變異量是該模態對 u/v/η 經分量 RMS 正規化及面積加權後「總變異」的解釋比例，
   不是海面高度或流速本身的百分比。

採用的主要圖表慣例來自：

- de Oliveira Júnior et al. (2022), *Ocean Science*, DOI
  [10.5194/os-18-1183-2022](https://doi.org/10.5194/os-18-1183-2022)：海表流 SVD 空間
  向量與對應時間模態分開呈現，箭頭按規則格點抽稀。
- Song et al. (2025), *Ocean Science*, DOI
  [10.5194/os-21-3361-2025](https://doi.org/10.5194/os-21-3361-2025)：SVD 以標量底色
  疊加流速向量，並配對 PC 時序。
- Volkov et al. (2022), *Ocean Science*, DOI
  [10.5194/os-18-1741-2022](https://doi.org/10.5194/os-18-1741-2022)：PC 標準化後，以每
  1 個 PC 標準差對應的物理回歸幅度呈現空間圖。

完整書目、官方文章與 PDF 連結、實際核對的章節／圖號、套用方式、與來源方法的差異，
集中記錄於 [`docs/svd_figure_reference_log.md`](docs/svd_figure_reference_log.md)；
引用管理軟體可直接匯入
[`docs/svd_figure_references.bib`](docs/svd_figure_references.bib)。未來新增或更換
圖表慣例時，應同步更新這兩份檔案與 `figures/plot_metadata.json` 的來源紀錄。

## 六區 2024–2025 表層 SVD 學術成果報告

正式成果報告由
[`scripts/reporting/build_six_regions_surface_svd_report.py`](scripts/reporting/build_six_regions_surface_svd_report.py)
產生。產生器只讀 `work/server_results/2024_2025/svd/<run-id>` 內已發布的科學陣列，及
`work/server_results/2024_2025/svd_figure_bundles/<run-id>/academic_report_ready_v8`
圖集；不重新讀取 surface cache、不修改缺值、不重新求解 SVD。報告依六區 batch 順序
完整納入平均場、解釋變異量、前五模態空間回歸場及前五標準化 PC，共 72 張 PNG。
所有平均場與空間模態均強制使用檔名含 `_with_vector_scale.png` 的版本，避免交付時遺漏
箭頭比例尺。

報告方法章逐步記錄共同有效遮罩、短缺口限制、時間軸 canonicalization、距平、u/v 共同
速度 RMS 與 η RMS 正規化、格點面積平方根權重、空間共變異矩陣特徵分解、與薄型 SVD
的等價關係、PC 標準化、空間係數、物理回歸場、符號慣例及候選海廢輸送模態判準；RMS、
loading、PC、EV、正交性與薄型 SVD 均先以白話定義再列公式。聚焦版移除命令列執行流程，
但保留足以說明資料轉換與矩陣求解的數學契約。聚焦版將六個研究海域視為獨立分析單元，
每章自行交代邊界、樣本、正規化尺度、平均流、解釋變異量、前五模態及 PC 時序，不設
區域合併比較章，也不收錄後續工項或內部成果追溯附錄；參考文獻固定由新頁開始。正文、
標題、公式、表格及圖說的 DOCX 字型均直接指定為「標楷體-繁」，正文為黑色 12 pt，且
正式版不設頁首；既有 v8 PNG 為版本化成果圖，不由報告產生器重繪或改字型。

使用鎖定的文件依賴環境執行：

```bash
/path/to/codex-primary-runtime/dependencies/python/bin/python3 \
  scripts/reporting/build_six_regions_surface_svd_report.py
```

預設輸出為：

```text
outputs/reports/指定海域_流場模態萃取時序變化與主導海廢輸送候選模態_聚焦版_v2_2024-2025.docx
```

原始 1.0 版
`outputs/reports/六指定海域_表層流場多變量SVD成果與海廢輸送意涵_2024-2025.docx`
保留不變；聚焦版使用不同檔名，執行產生器時不會覆寫既有報告。

產生器在儲存前會稽核 72 張嵌入圖片與所有固定寬度表格。交付前仍應將 DOCX 渲染為逐頁
PNG 或 PDF，核對目錄頁碼、中文 fallback、公式、圖說、參考文獻分頁及每一張向量比例尺；
若增刪正文或圖件，必須重新渲染並同步更新靜態目錄頁碼。

若正式 DOCX 已由研究人員在 Word 內人工修訂，後續專有名詞格式不得重新執行整份報告產生器
覆寫。可使用 `scripts/reporting/normalize_docx_technical_terms.py` 直接修改既有 OOXML：首次出現
採「中文全名（English Full Name, ABBR）」；沒有通用縮寫者採「中文名稱（English Term）」；
後續直接使用縮寫或已定義名稱。工具會先建立完整備份，且每個目標片段必須唯一命中才會寫檔，
以保留人工調整的段落、圖片、樣式與分頁。

若只需補強正式 DOCX 的「10.7 共用方法限制」，使用
`scripts/reporting/expand_docx_common_method_limitations.py` 局部修改。工具將原有五個高度濃縮的
限制擴寫為七項，分別交代模式資料代表性、缺值、分析邊界與尺度、聯合 EV、線性正交基底、
區域彙整指標及兩年樣本穩健性；每項均說明限制來源、對結果的可能影響與正確解讀界線。
工具不重建文件，且必須逐字命中鎖定版 10.7 才會寫入；新增內容造成分頁位移時，應先完成
逐頁渲染，再以 `--reference-page` 回填靜態目錄中的參考文獻頁碼。

## 本機驗證

```bash
UV_CACHE_DIR=/private/tmp/ocm-svd-analysis-uv-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run python3 -m unittest discover -s tests -v
```

測試套件以合成 schema 3 surface cache 驗證五模態輸出、面積權重正交性、完整重建、
短缺值插補、逐階段計時、單區 immutable 重繪、兩區平行重繪與拒絕覆寫；另以 paired
native/surface cache 驗證固定 z 線性內插、不外插、`eta_m` 原值配對，以及表層與三個
固定深度共用格點／時間交集。不需要大型 SERVER 資料或 raw NetCDF。

六層聯合直接 SVD 的測試另外同時覆蓋 dense LAPACK 與強制 PROPACK streaming 兩條路徑，
檢查六個速度層、唯一 eta、設定要求的奇異三元組（正式水柱設定為 100 組、圖面預設前 20 組）、左右殘差，以及每一模態的六張獨立速度圖、
一張獨立 eta 圖與一張獨立 PC 圖。若要以
本機保存的後灣單日 paired cache 驗證真實 I/O，使用專為開發設計的 trial 設定：

```bash
export OCM_NATIVE_ROOT=/Users/mustlab/Workspace/OCM-Data-Preprocessing/preprocessed/trials/202501_days_01/ocm_native
export OCM_SURFACE_ROOT=/Users/mustlab/Workspace/OCM-Data-Preprocessing/preprocessed/trials/202501_days_01/ocm_surface
export SVD_OUTPUT_ROOT=/private/tmp/ocm-water-column-trial

UV_CACHE_DIR=/private/tmp/ocm-svd-analysis-uv-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run ocm-svd-water-column \
  --config configs/houwan_nmmba_flow_domain_water_column_svd_trial_202501_day01.json \
  --native-root "$OCM_NATIVE_ROOT" \
  --surface-root "$OCM_SURFACE_ROOT" \
  --output-root "$SVD_OUTPUT_ROOT" \
  --allow-trial \
  --no-figures
```

此 trial 只驗證完整 flow-domain 的 paired I/O、固定深度內插、矩陣建立及直接 SVD；它僅有
一天資料，不能用來評估兩年變異結構或替代 SERVER 正式成果。

## 下一階段

完成 2024–2025 六區資料品質與圖面 review 後，可依同一個六區分析單元版本新增 40 小時
低通的次潮 SVD、季節比較與 block bootstrap。既有雙年度設定不可直接改寫年份或 AOI；
AOI polygon 或 cell fraction 若再調整，也必須先在前處理專案建立新的分析單元版本，再產生
新的 run ID。
