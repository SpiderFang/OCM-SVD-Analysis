# 需求與來源基線

## 1. 文件控制

| 欄位 | 值 |
|---|---|
| 文件版本 | `1.0.0-replan` |
| 稽核日期 | 2026-07-21（Asia/Taipei） |
| 用途 | 固定需求來源、實檔事實與可追溯的規格修正，不記錄尚未驗證的研究結論 |
| 排除來源 | 本目錄中先前未提交的 README 與規格草稿；新版不得以其敘述作證據 |

## 2. 證據優先順序

發生矛盾時依下列順序處理：

1. 實際 OCM/NWW3 檔案的表頭、維度、值數量與時間軸。
2. 使用者最新明確指示及已核定的跨對話設計決策。
3. 主工作計畫書第 2-4 節、第三章時程與第四章交付要求。
4. `timeline.txt` 的分析工作月份與 DONE 狀態。
5. OCM 截圖與 NWW3 欄位附件等格式說明。
6. 原始研究論文、官方模式手冊及軟體文件。

附件中的概略名稱、單位或「二進位」等說明若與實檔不符，讀檔契約以實檔為準，但須保留差異紀錄並向資料提供者確認物理語意。

## 3. 來源清單與可用範圍

| 來源 | 路徑/識別 | 本次使用方式 | 限制 |
|---|---|---|---|
| 主工作計畫書 | `/Users/mustlab/Downloads/工作計畫書(629).pdf` | 視覺檢查與逐頁文字擷取 | 48 個 PDF 實體頁；文件頁碼與 PDF 頁碼差 5 頁 |
| 自訂時程 | `/Users/mustlab/Downloads/timeline.txt` | 逐行讀取 | 只有月份，未列負責人、輸入、驗收與結案報告期 |
| OCM 欄位截圖 | `/Users/mustlab/Downloads/OCM.png` | 視覺核對變數名稱 | 不含完整單位、方向與缺值契約 |
| NWW3 欄位附件 | `/Users/mustlab/Downloads/NWW3.pdf` | 視覺核對 17 種欄位 | 部分物理名稱與實檔表頭不同；附件未呈現 transfer-file layout |
| OCM 格式樣本 | `/Users/mustlab/Downloads/CWA-OCM/2025/01` | 直接讀 31 個 NetCDF 表頭與小範圍數值 | 只有 2025-01，不代表兩年涵蓋率 |
| NWW3 格式樣本 | `/Users/mustlab/Downloads/CWA-NWW3/2025` | 直接讀 4 個 tar.gz 的成員與表頭 | 只有 2025-01-01 四個 archive，不代表兩年涵蓋率 |
| 跨對話決策 | Codex task `019f8023-8b63-7102-9ecd-cc4a67e56051`，「龜山島與貢寮共用域分離分析」 | 採用 flow domain 與 AOI 分離原則 | 是軟體設計決策，不取代研究團隊對正式 bbox/AOI 的核定 |

使用者說明 2024、2025 完整資料位於 SERVER；本次未取得 SERVER 根路徑與完整清單，因此任何「兩年完整」狀態都必須由正式 inventory 任務判定。

## 4. 主工作計畫書需求

### 4.1 頁次對照

| 內容 | 文件頁碼 | PDF 實體頁 |
|---|---:|---:|
| 作業範圍 | 4–7 | 9–12 |
| OCM 與 ADCP 合理性驗證 | 20 | 25 |
| 第 2-4 節 | 26–31 | 31–36 |
| 預定進度甘梯圖 | 32 | 37 |
| 預期成果 | 34–35 | 39–40 |
| 參考資料 | 39–40 | 44–45 |

### 4.2 五處海域

| 研究區 | PDF 文字需求 | 規格化解讀 |
|---|---|---|
| 宜蘭龜山島 | 西側海域，調查水深 5–10 m | 現場作業區是受體/AOI 的依據，不等於完整背景 flow domain |
| 新北貢寮 | 保護區適宜海域，水深 10 m 以上 | 與龜山島共享東北臺灣背景環流，可共用前處理域但分開分析 |
| 新竹外海 | 南寮、香山人工魚礁、頭前溪口，水深 10–30 m | 一個 flow domain 下至少三個子 AOI，分別保存結果與比較 |
| 屏東海生館後灣 | 海生館外海後灣周邊，水深 5–10 m | 近岸表層與近底機制都要分析，需特別處理邊界與水深 |
| 連江 | 北竿 3 處、南竿 4 處歷史清除熱點；最終選南竿或北竿周邊 | 共用連江背景域，七個候選 focus AOI；正式選址待核定 |

### 4.3 第 2-4 節明列的研究方法

1. 以 2024–2025 CWA-OCM 三維海流與 CWA-NWW3 二維波浪為主要資料，2026 視需求滾動納入。
2. 將 OCM 非結構網格與 NWW3 格點資料重採樣到「高於一公里解析度」的均勻網格；本規格採保守可驗收定義為格距 `≤ 1 km`。
3. 以 SVD 萃取流場主導模態、解釋變異與時間係數。
4. 由表層二維速度梯度的應變率張量偵測 TRAP 核心與積分曲線，分析強度、空間分布與生命週期。
5. 以三維 OCM、波浪 Stokes drift、沉降/上浮與次網格擴散進行系集逆向溯源。
6. 追蹤粒子離開關注域時的邊界穿越位置，以 KDE 形成潛在來源足跡，並分析傳輸路徑與聚集熱區。

### 4.4 工作計畫書內部需要修正或澄清之處

| 原文件敘述 | 問題 | 新版實作規則 |
|---|---|---|
| `space × time` 矩陣 SVD 後，U 的「行向量」代表 SVD | 線性代數敘述錯置 | U 的欄向量是空間 SVD；`ΣVᵀ` 是未正規化 PC |
| 時空矩陣先稱 A，後稱 X | 符號不一致 | 全規格使用 `X`，距平使用 `X'` |
| 「高達 1,000 組」；另列 10 種沉降 × 20 受體 × 50 到達時間 | 乘積為 10,000 | 未決策前不得宣稱固定情境數；若上限 1,000，採分層/LHS 設計 |
| 隨機微分方程以 RK4 積分並另加隨機漫步 | RK4 只處理確定性漂移 | 確定性項用 RK4/RK45；擴散項用 Euler–Maruyama 或一致的 SDE split step |
| 逆向速度取負並加入擴散 | 不等同嚴格的反向擴散機率 | 主要成果稱條件式反向足跡；以合成真值、正反向一致性與敏感度限制解釋 |
| 用 `Hs, Tp, θ` 的深水單色波 Stokes 式 | 五站多為近岸，且 NWW3 沒有完整頻譜 | 基準採有限水深 bulk 近似；深水式為對照，並明示 bulk 近似偏差 |
| TRAPs 同時說明海漂及海底聚集 | TRAP 是表層二維瞬時結構 | 只直接支援表層漂移；海底結論由三維近底軌跡、底部接觸與觀測驗證建立 |
| 所有三維資料直接統一 1 km | 可能造成巨大 I/O，且規則格網不保留原生拓撲 | 保存裁切 native 3D；只對核定深度/HAB 產生 regular 3D 衍生層 |

## 5. `timeline.txt` 原始基線

| 原文 | 標準化月份 | 初始狀態 |
|---|---|---|
| 資料搜集 6月-7月 (DONE) | 6–7 月 | 行政 DONE；技術 QC 尚需正式確認 |
| 流場模態萃取 7-8月 | 7–8 月 | 待執行 |
| 數值模式建置(Lagrangian 逆向溯源) 8月-9月 | 8–9 月 | 待執行 |
| 數值模擬與計算 (Lagrangian 逆向溯源) 9月-10 月 | 9–10 月 | 待執行 |
| 瞬態吸引剖面分析 9月-10月 | 9–10 月 | 待執行 |
| 熱區辨識與動力成因探討9月-11月 | 9–11 月 | 待執行 |

主工作計畫書另載明計畫至 2026-12-15，且結案報告需在分析期間同步撰寫。新版任務表不改變上述六項月份，而是在 11 月後增加「結果凍結、審查、封存」交付工作。

## 6. 跨對話設計決策

指定對話的最新結論如下，已成為本規格的架構約束：

1. OCM raw data 先以較大的共用 `flow_domain` 保存必要變數到 `.npy` 快取，以保留完整背景環流。
2. SVD、統計、報告圖與作業區比較前，從共用快取套用 `analysis_aoi` 或 `focus_bbox`。
3. SVD 的空間域必須在建立資料矩陣前確定；不能先對整個共用域做 SVD，再把 SVD 圖硬切成龜山島/貢寮來解釋。
4. bbox 可用於快速切片；貼岸、港灣、島體與不規則作業區以 polygon mask 為正式邊界。
5. 共用域允許相鄰研究區的水動力背景重疊，AOI 則負責避免統計重複與解釋混淆。

對話提到的東北臺灣候選大框與貢寮 focus bbox 只作設計示例，不在本規格中升格為正式地理契約。

## 7. OCM 實檔稽核

### 7.1 檔案、格式與時間

| 項目 | 稽核結果 |
|---|---|
| 樣本檔數 | 31 個：`20250101_schout.nc` 至 `20250131_schout.nc` |
| 合計大小 | 246,176,193,064 bytes（約 229.27 GiB） |
| 單檔大小 | 約 7.92–7.96 GB |
| NetCDF 格式 | `NETCDF4_CLASSIC` |
| 宣告規範 | CF-1.0、UGRID-1.0 |
| 單檔時間步 | 24 |
| 2025-01 時間步 | 744，全部唯一、連續每小時 |
| 第一/最後時間 | 2025-01-01 01:00 UTC / 2025-02-01 00:00 UTC |
| 重要規則 | 不能由日檔名假設 00:00 開始；必須解析 `time.units` |

### 7.2 維度與網格

| 維度 | 大小 | 意義 |
|---|---:|---|
| `time` | 24/日 | 每小時輸出 |
| `nSCHISM_hgrid_node` | 508,456 | 非結構水平節點 |
| `nSCHISM_hgrid_face` | 988,434 | 面元素 |
| `nSCHISM_hgrid_edge` | 1,497,045 | 邊 |
| `nSCHISM_vgrid_layers` | 48 | 最大垂向層數 |
| `nMaxSCHISM_hgrid_face_nodes` | 4 | 每面最多四節點 |

樣本全域經緯度約 105.5873–148.0329°E、4.4517–46.4921°N；深度 5–9816.328 m。這說明正式工作必須先裁切物理一致的 flow domain，不能把全域兩年 3D 陣列一次展開。

### 7.3 核心變數契約

| 變數 | 形狀 | 實檔屬性與實作要求 |
|---|---|---|
| `SCHISM_hgrid_node_x/y` | `(node)` | 經緯度，degrees_east/degrees_north |
| `SCHISM_hgrid_face_nodes` | `(face,4)` | `start_index=1`、fill=-99999；轉為 0-based 時保留填值 mask |
| `depth` | `(node)` | meters、positive down |
| `node_bottom_index` | `(node)` | 1-based，樣本範圍 1–46 |
| `time` | `(time)` | 每檔有獨立 `seconds since ... +0000` |
| `zcor` | `(time,node,layer)` | 實際垂向 z；沒有 units 屬性，需供應者確認 |
| `hvel` | `(time,node,layer,2)` | 水平兩分量；沒有 units 屬性 |
| `vertical_velocity` | `(time,node,layer)` | 沒有 units/positive 屬性 |
| `elev` | `(time,node)` | 沒有 units 屬性；依名稱推測不可取代正式契約 |
| `wind_speed`、`dahv` | `(time,node,2)` | 兩分量；沒有 units 屬性 |
| `temp/salt/water_density/diffusivity` | `(time,node,layer)` | 沒有 units 屬性 |
| `wetdry_elem` | `(time,face)` | 樣本首時次只有 0；0/1 語意仍需確認 |

主要時變欄位使用 `9.96921e36` 作 `missing_value`。讀檔器必須將 NetCDF masked array 轉成數值陣列與明確布林 mask；填值不可進入插值、梯度、SVD 或粒子速度。

### 7.4 垂向座標關鍵發現

- `sigma` 的 48 筆皆為 0；`Cs` 的 48 筆皆為 NaN；`sigma_h_c` 與 `sigma_maxdepth` 為 0。
- 因此不可用一般 sigma 公式重建深度，必須直接使用逐時逐節點 `zcor`。
- 5 m 節點在抽樣時只有 5 個有效 z 層；約 51.86 m 節點有 13 層；最深節點有 48 層。
- 同一 layer index 在不同節點與時間不代表相同物理深度。表層取最高有效 z，近底取最低有效 z 或指定 HAB；固定深度分析需用 `zcor` 垂向內插。

### 7.5 OCM 尚未確認的契約

1. `hvel`、`vertical_velocity`、`zcor`、`diffusivity`、`wind_speed` 的正式單位與正方向。
2. `wetdry_elem=0/1` 的 dry/wet 定義，以及 last-wet 值的使用限制。
3. 2024–2025 每月檔案命名、重跑版本、缺檔與網格版本是否一致。
4. OCM 速度是否已含潮汐、波流交互作用或其他近岸效應；避免重複加入物理項。

## 8. NWW3 實檔稽核

### 8.1 archive 與有效時間

| archive | bytes | 成員數 | `.hs` 最早–最晚有效時間 |
|---|---:|---:|---|
| `nww3_grd3_2025010102.tar.gz` | 38,225,583 | 1,649 | 2024-12-31 12:00 – 2025-01-04 12:00 |
| `nww3_grd3_2025010108.tar.gz` | 38,208,213 | 1,649 | 2024-12-31 18:00 – 2025-01-04 18:00 |
| `nww3_grd3_2025010114.tar.gz` | 39,273,592 | 1,649 | 2025-01-01 00:00 – 2025-01-05 00:00 |
| `nww3_grd3_2025010120.tar.gz` | 39,356,118 | 1,649 | 2025-01-01 06:00 – 2025-01-05 06:00 |

每個 archive 為 17 種欄位 × 97 個逐時有效時間。成員有效時間可能早於 archive 名稱時間，且四個 archive 大量重疊；因此不能只從 archive 名稱推導有效時間或盲目覆蓋。正式讀檔器以成員第一列的日期/時間為 `valid_time`，另保存 archive stem 作 `cycle_tag`，待供應者說明後才決定去重優先序。

### 8.2 實際 transfer-file 格式

實檔不是附件文字容易讓人理解的 raw binary，而是 WAVEWATCH III transfer file：第一列為 ASCII 表頭，後續為縮放整數 ASCII。範例：

```text
WAVEWATCH III 20250104 100000 117.60 123.90 253 20.80 26.70 237 .hs 0.0100 m 3 1 (1X,32I4) -999
```

表頭欄位依序包含模型、有效日期/時間、經度範圍與點數、緯度範圍與點數、欄位、scale、unit、IDLA、IDFM、格式字串與 missing code。解碼順序必須是：

1. 先讀整數並以 `-999` 建立缺值 mask。
2. 對非缺值套用 `physical_value = integer × scale`。
3. `IDLA=3` 表示逐列由 top 到 bottom；若內部緯度採遞增，需翻轉 y 軸。
4. 標量場值數為 `253 × 237 = 59,961`；`.wnd` 值數為 119,922，必須解成兩個分量場。

原生網格為 117.60–123.90°E、20.80–26.70°N、0.025° 間距。所有研究區都在格網範圍內，但近岸有效格點與遮罩仍需逐 AOI 檢查。

### 8.3 17 種欄位與實檔 scale

| 副檔名 | 實檔表頭代碼 | scale/unit | 用途與限制 |
|---|---|---|---|
| `.hs` | `.hs` | 0.01 m | 有效波高；Stokes bulk 基準 |
| `.dir` | `.dir` | 1 degree | 平均波向；圓周量 |
| `.t` | `.t` | 0.01 s | 平均波週期 |
| `.dp` | `.dp` | 1 degree | 尖峰波向；與 `fp` 配對 |
| `.fp` | `.fp` | 0.001 Hz | 尖峰頻率，`Tp=1/fp` |
| `.wnd` | `.wnd` | 0.1 m/s | 兩分量，順序需確認；不能當單一風速/風向陣列 |
| `.l` | `.l` | 1 m | 實檔表頭對應平均波長；附件稱 peak wavelength，需確認 |
| `.spr` | `.spr` | 0.1 degree | 波向分散度 |
| `.ptp0-2` | `.ptp` | 0.01 s | 三個 partition peak period |
| `.pth0-2` | `.pth` | 1 degree | 實檔顯示 partition 方向；附件把它描述為波高，明顯衝突 |
| `.phs0-2` | `.phs` | 0.01 m | 三個 partition wave height |

`.pth*` 是本次最重要的附件/實檔差異之一：副檔名及表頭 unit 顯示 direction，而附件表格稱 wave height。新版程式不得依附件把 `.pth*` 當公尺；必須以供應者資料字典與官方 transfer-file 定義確認。

### 8.4 波向契約

WAVEWATCH III 官方手冊將 `DIR` 定義為 meteorological convention，`DP` 以相同方式定義。正式轉換前必須以供應者範例或已知場驗證角度是「來向」還是「去向」。程式內部使用向東/向北傳播向量，空間與時間插值以 `cosθ/sinθ` 分量進行，禁止直接對 359° 與 1° 做純角度線性平均。

## 9. 資料規模與資源推估

- OCM 2025-01 約 229.27 GiB；若兩年每月接近此量級，raw OCM 約為 5.4 TiB 級，尚未含重複版本、NWW3 與衍生品。
- OCM 全域單時次 `hvel` 理論元素數約 `508,456 × 48 × 2`；兩年全域一次載入不可行。
- 前處理必須逐檔、逐時間塊、逐 flow domain 寫入；大型 `.npy` 以 memory map 讀寫。
- 規則 1 km 格網只在共用域與核定深度產生，不為每個小 AOI 重複存一份同樣背景資料。
- 正式運算前要以 pilot 量測每時步速度、記憶體與輸出膨脹率，再凍結工作站/HPC 資源估算。

## 10. 外部方法與官方格式依據

以下連結作為實作方法的主要參考，不能取代本地資料契約：

- [Kunz et al. (2024), Transient Attracting Profiles in the Great Pacific Garbage Patch](https://doi.org/10.5194/os-20-1611-2024)：TRAP 核心、曲線、追蹤與生命週期。
- [Serra & Haller (2016), Objective Eulerian coherent structures](https://doi.org/10.1063/1.4951720)：TRAP/OECS 理論基礎。
- [van Sebille et al. (2018), Lagrangian ocean analysis](https://doi.org/10.1016/j.ocemod.2017.11.008)：三維粒子與路徑統計的基本實務。
- [Batchelder (2006), FITT/BITT trajectory modeling](https://doi.org/10.1175/JTECH1874.1)：正向/反向軌跡的差異與限制。
- [Durgadoo et al. (2019/2021), Strategies for simulating the drift of marine debris](https://doi.org/10.1080/1755876X.2019.1602102)：海流、Stokes drift、浮力與正反向策略敏感度。
- [WAVEWATCH III v5.16 manual](https://polar.ncep.noaa.gov/waves/wavewatch/manual.v5.16.pdf)：transfer file、IDLA/IDFM 與波浪參數定義。
- [Zhang et al. (2016), SCHISM](https://doi.org/10.1016/j.ocemod.2016.05.002)：模式與非結構網格背景。
- [Delandmeter & van Sebille (2019), Parcels v2.0](https://doi.org/10.5194/gmd-12-3571-2019)：Lagrangian 場內插與格網支援的設計參考。
- [van den Bremer & Breivik (2018), Stokes drift](https://doi.org/10.1098/rsta.2017.0104)：Stokes drift 定義與近似限制。

## 11. 基線結論

1. 本機資料只能證明格式，不能證明 SERVER 上兩年資料完整。
2. OCM 的垂向座標必須用 `zcor`；固定 layer index 不能當成固定物理深度。
3. NWW3 需實作 transfer-file ASCII 解碼、IDLA 翻轉、scale/missing、重疊 cycle 去重與向量波向轉換。
4. flow domain 與 analysis AOI 必須分層；SVD 一定在 AOI 切片後才建立矩陣。
5. 表層 TRAP、三維粒子與近底聚集證據必須分開驗證再綜整。
6. 情境數、地理邊界、受體、單位、NWW3 cycle 與廢棄物參數仍是正式計算的決策閘門。
