# 驗證、圖表、動畫與結案報告規格

## 1. 目的

本文件確保成果不只「能計算」，還能回答研究問題、量化不確定性，並以學術研究可審查、可重現且不誤導的方式呈現。

## 2. 驗證層級

| 層級 | 目的 | 例子 | 失敗後處置 |
|---|---|---|---|
| V0 格式/契約 | bytes 是否被正確解讀 | NWW3 scale/IDLA、OCM start index/time/missing | 阻止標準化產品進入 ready |
| V1 數值單元 | 演算法是否符合解析真值 | SVD、gradient、RK4、擴散方差、KDE | 阻止模組 release |
| V2 整合/守恆 | 模組串接是否保留單位、mask、時間與幾何 | raw→cache→AOI→derived round trip | 阻止工作流程 release |
| V3 模式–觀測 | forcing 是否合理反映現場 | OCM–ADCP、波浪–浮標、溫鹽背景 | 降低結論可信度或限制時窗 |
| V4 預測/機制 | 熱區與路徑是否與獨立資料一致 | 聲納/ROV 點位、已知來源合成案例 | 結論降級為假說 |
| V5 重現性 | 他人是否能重建結果 | 乾淨環境、manifest、final figures | 不得結案 |

每一個研究結論都要在 `validation_matrix.parquet` 對應至少一個測試/觀測證據與限制；沒有證據的敘述只能列為討論或未來工作。

## 3. 資料與前處理驗證

### 3.1 OCM

- 每檔時間、shape、變數與屬性 schema 比較；grid/schema 變更立即分版。
- 每月第一、隨機及最後時次抽樣 raw→標準化快取，比對時間、節點、層、u/v/w/z/elev/mask。
- 畫出 domain/AOI、來源節點、規則格點、海陸/乾濕 mask、最近來源距離與內插支撐。
- 每個固定 z/HAB 層抽查淺/中/深水柱，圖示原始 `zcor` 與內插點；海底以下必為無效。
- 表層取樣圖同時顯示 `surface_z`，證明不是固定 layer index。
- 對解析線性向量場，水平內插應在浮點容許誤差內復原；對非線性場報告交叉驗證誤差。

### 3.2 NWW3

- 每個欄位/成員驗證表頭、值數、scale、missing、IDLA/IDFM。
- 以已知 2×3 人工格網測試 IDLA top-to-bottom 與內部緯度遞增翻轉。
- `.wnd` 測試必須證明兩個 253×237 場的切分順序；未確認順序前不可命名 u/v。
- 波向 circular test：359°/1° 插值應接近 0°；北/東來向轉傳播向量的基準案例需由資料提供者簽核。
- 對重疊 archive 產出各欄位相鄰 cycle 的 bias/RMSE/max map；去重規則變更需重建產品。
- `.pth*` 依實檔 degree 處理；若資料字典另有解釋，須以新 major schema 重建。

### 3.3 涵蓋率

每站、每變數、每深度、每月輸出：

- 預期/實得時間步、有效率、重複、最大連續缺口。
- AOI 內平均/最小空間有效率。
- OCM/NWW3 同時可用窗口。
- 因格網、海陸、乾濕、深度不足與資料缺口造成的 unavailable 分解。

正式分析窗需事前登錄；不得看完結果後只挑資料完整或機制顯著的日期。

## 4. SVD 驗證

### 4.1 數值檢查

- `sum(explained_variance)=1`（僅對實際保留的非零奇異值範圍，容許 `1e-10` 級誤差）。
- `UᵀU≈I`、`VᵀV≈I`；加權回復後檢查 `ΦᵀWΦ≈I`。
- 全模態重建誤差接近浮點誤差；截斷重建誤差隨模態數單調不增。
- rank-k 合成向量場復原已知子空間；不要只逐模態比正負號。
- 固定種子 randomized SVD 與 deterministic SVD 在小資料的 EV/子空間角一致。

### 4.2 統計穩定性

- 時間 block 長度依 PC 自相關/積分時間尺度設定，不以獨立逐時樣本 bootstrap。
- 報告 EV 的 95% bootstrap interval、SVD pattern correlation、principal/subspace angle。
- 相近 eigenvalues 符合 North sampling-error 範圍時標記 degenerate pair，解釋其子空間而非固定 SVD1/SVD2 形狀。
- 比較 2024、2025、兩年合併、季節與逐時/低通版本；模態不穩定時不得作唯一物理解釋。

### 4.3 物理解讀 QA

- 圖中同時提供平均流；SVD 是距平 pattern，不能被寫成「實際流向」。
- PC 正/負相位的重建合成須與原始極端時次抽樣一致。
- surface、fixed-z、HAB、full-3D 的標題/caption 明示深度定義。
- 比較 AOI 前確認權重、時間窗、濾波與標準化一致；否則只作並列描述。

## 5. TRAP 驗證

### 5.1 合成流場

| 測試場 | 預期結果 |
|---|---|
| 匀速平移 | `S=0`，不產生顯著 TRAP |
| 剛體旋轉 | symmetric strain 接近 0，不產生吸引 TRAP |
| 線性純應變 | eigenvalue/eigenvector 與解析值一致，曲線方向正確 |
| 二維鞍點 | 核心落於解析位置，TRAP 沿最大伸張方向 |
| 加旋轉/平移座標變換 | 轉回原座標後結構一致 |
| 海岸 mask 人工場 | 曲線不得穿越陸地或無資料 |

### 5.2 敏感度與追蹤

- 對 grid spacing、0–3 格平滑、核心門檻、積分步長、最長曲線與 coastline buffer 建參數矩陣。
- robust TRAP 定義須事前登錄，例如在多數合理設定中都有鄰近核心/曲線；門檻本身在報告列出。
- 人工平移/變形曲線測試 track association、split/merge、gap closing。
- 粒子–TRAP 統計以時間/空間置換或 matched control 建 null，不能只呈現靠近比例。

## 6. 粒子模式驗證

### 6.1 解析/統計案例

| 項目 | 測試 |
|---|---|
| 匀速平流 | 位置誤差隨 dt 符合方法階數 |
| 剛體旋轉 | 半徑與週期保持、閉合誤差量化 |
| 剪切流 | 解析軌跡一致 |
| 時變流 | 空間/時間內插與解析解比較 |
| 沉降/上浮 | z 位移與 `w_b × t` 一致，觸底/海面時間正確 |
| 水平/垂向擴散 | ensemble displacement variance 接近 `2Kt`，均值無偏 |
| 開放邊界 | first crossing 位置/時間在步內內插後正確 |
| 岸/海床 | strand/reflect/deposit 事件不重複且不穿越 |
| restart | 中斷前後結果、particle ID、seed 與一次跑一致 |

### 6.2 收斂與系集充分性

- 至少測 `dt, dt/2, dt/4`，比較終點誤差、boundary ranking、path-density correlation 與 90% HDR overlap。
- ensemble size 逐倍增加，監看 HDR 面積、來源 ranking、網格密度變異；達到事前容許變化才凍結。
- KDE bandwidth 與軌跡輸出間隔分開測試；不能用平滑掩蓋粒子不足。
- flow domain 擴大測試，確保人工邊界不主導來源結論。

### 6.3 正向–反向合成驗證

1. 從已知來源 polygon 依指定日期/深度向前釋放粒子。
2. 選取到達受體者，加入符合觀測的不確定性。
3. 由受體反向執行同一 forcing 情境。
4. 評估來源真值是否落入 50/75/90% HDR、來源 ranking、質心距離與 coverage。
5. 分別比較 deterministic、加擴散、no-Stokes、有限水深/深水與不同邊界。

這個測試決定能否將結果稱為「來源足跡」。未通過時只能呈現反向軌跡敏感度。

## 7. 模式–觀測驗證

### 7.1 OCM–ADCP

依 ADCP 航跡與 bin depth/HAB，把 OCM 插值到相同 UTC、位置、深度與平均窗。至少報告：

- u/v bias、MAE、RMSE、Pearson/Spearman correlation。
- speed bias/RMSE；方向 circular bias/MAE，只在 speed 高於噪音門檻時計算。
- complex/vector correlation 或 vector RMSE。
- 沿測線/深度剖面、Taylor/target diagram（若樣本數足夠）。
- 95% interval 使用時間/航段 block bootstrap。

ADCP 是短期移動調查，不能單獨驗證兩年 climatology。觀測精度、船速/姿態修正與時間錯位要進誤差討論。

### 7.2 CTD

CTD 用於檢查 OCM 溫鹽/密度垂向結構與水團背景，不直接驗證速度。報告 profile bias/RMSE、混合層/躍層深度差與站點不確定性；若 OCM 溫鹽單位未確認，不執行量值結論。

### 7.3 NWW3

若有浮標/現場波浪，對 Hs、Tp/fp、方向做時空對位與上述標量/circular metrics。若沒有：

- 做物理範圍、land mask、cycle continuity、`.l`–dispersion relation 與 OCM 水深一致性。
- 在報告明示「格式/QC，不是獨立觀測驗證」。

### 7.4 廢棄物熱區

有調查 effort/覆蓋時：

- 以空間 block cross-validation 評估 hotspot ranking、AUC/PR、top-k capture、distance-to-hotspot。
- 以 effort offset 或可探測面積校正；同一 survey 的相鄰點不跨 train/test。
- 廢棄物類別分層，避免漂浮物與海底漁具混合。

只有 presence 點時：

- 使用 distance、top-k capture、背景抽樣敏感度與定性疊圖。
- 不報告一般分類 accuracy，也不把未調查格當 absence。

## 8. 不確定性與證據等級

### 8.1 不確定性來源

| 類別 | 最低敏感度 |
|---|---|
| 資料 | 2024 vs 2025、缺口/去重、OCM/NWW3 時間與單位 |
| 空間 | flow domain 大小、AOI、1 km/較粗格距、海岸 mask |
| 垂向 | surface/fixed-z/HAB、z 內插、底部處理 |
| SVD | 時間窗、去均值/濾波、mask、模態退化 |
| TRAP | 平滑、梯度、核心/曲線/追蹤門檻 |
| 粒子 | dt、ensemble、Kh/Kz、浮沉、Stokes、windage、邊界、horizon |
| 熱區 | bin、bandwidth、normalization、來源 prior |
| 觀測 | 位置/時間/深度、儀器誤差、effort、分類誤差 |

### 8.2 證據等級用語

| 等級 | 條件 | 報告可用措辭 |
|---|---|---|
| A | 獨立觀測驗證 + 多方法/敏感度一致 | 「結果支持…為主要機制」 |
| B | 合成/數值驗證 + 參數穩健 + 部分觀測一致 | 「結果顯示…可能是重要機制」 |
| C | 模式內部一致但觀測不足或參數敏感 | 「模式情境指出…，仍需驗證」 |
| D | 探索性、資料或契約未確認 | 「初步假說/方法示範，不作量化結論」 |

每個站點的結論表需列證據等級、支持資料、矛盾資料與限制。

## 9. 學術圖表通用規格

### 9.1 輸出與版面

- 主檔：向量 PDF/SVG；點陣 PNG 至少 300 dpi，線稿/細小文字建議 600 dpi。
- 單欄寬預設 85 mm、雙欄 180 mm；在最終尺寸檢查文字、箭頭與線寬，不只在螢幕放大檢查。
- 最小文字建議 7–8 pt；同一報告固定字型族，輸出 PDF 嵌入字型。
- 多面板使用 `(a)`, `(b)`；caption 可獨立理解，含資料期、深度、單位、方法、樣本數、統計區間與限制。
- 所有圖由程式生成，禁止在簡報/繪圖軟體手工改數值、色階、箭頭或文字後冒充可重現成果。

### 9.2 色彩

- 連續非負量：感知均勻 sequential colormap（如 viridis/cividis）。
- 正負距平：以物理中點 0 對稱的 diverging colormap，兩端範圍相同。
- 類別：色盲友善、同時用線型/標記區分；不只靠紅綠。
- 禁止 rainbow/jet 作科學主圖；若沿用外部既有圖，caption 說明來源。
- 跨站/跨時比較使用固定色階；若必須各自縮放，標題/caption 明示，不允許視覺上直接比較強度。
- 灰階列印仍需能以線型、符號或明度辨識。

### 9.3 地圖

- 標示 CRS/投影、經緯度刻度、海岸線來源、比例尺；北向不是直上時加 north arrow。
- 顯示研究 AOI、受體、flow-domain 邊界時使用不同圖例，不能讓讀者誤認。
- bathymetry 用 m 並說明 positive down；海面/深度/HAB 命名一致。
- 速度箭頭需 quiver key（如 `0.5 m s⁻¹`），箭頭下採樣規則固定並避免遮住小島/核心。
- 陸地、無資料、資料不足與 0 值使用不同視覺語意；不可用相同白色混淆。
- 密度/KDE 圖同時給 denominator、cell area normalization 與 HDR contour；避免只以飽和熱圖暗示高信心。

### 9.4 SVD 圖

- 平均流、SVD loading 與 PC 分開 panel；loading 不是實際速度快照。
- 標題含產品、AOI、深度、期間、mode、EV%。
- u/v SVD 用箭頭或分量 panel；若以箭頭呈現，說明 normalization，不能標成 m/s 除非確實回復物理 scaling。
- PC 有 UTC/年月軸、0 線、單位/標準化、季節陰影與不確定區間。
- degenerate modes 以 pair/subspace 解釋，不任意固定先後。

### 9.5 TRAP 與粒子圖

- TRAP 核心、曲線、`s1` 背景與 surface velocity 使用互不混淆的圖例；`s1` 單位明示。
- 軌跡多時用透明度/hexbin/density，不把數十萬條線全部疊上；代表軌跡的選法需預先定義。
- 起點、終點、邊界事件、受體、來源 AOI 有不同符號。
- 不確定範圍以 HDR contour/帶狀區間呈現，不只畫平均線。

## 10. 動畫規格

### 10.1 主檔與技術格式

- 學術/簡報主檔：MP4，H.264，`yuv420p`，1080p 或依圖面比例的等效清晰度。
- GIF 只作快速預覽，不作唯一交付；必要時限制尺寸與 fps。
- frame PNG 可選擇封存關鍵幀，不必保留所有中間幀，但需能由 manifest 重建。

### 10.2 視覺一致性

- 所有幀固定 extent、投影、色階、colorbar、箭頭 scale、圖例位置與字體。
- 清楚顯示 UTC 時間；若另顯示臺灣時間，兩者標籤不可混淆。
- 顯示真實 frame interval、播放加速倍率與資料缺口；缺時次不可用上一幀悄悄補上。
- 粒子動畫需標示仍存活/已沉積/已出界粒子數；TRAP 動畫顯示 track/core 與強度。
- 第一/最後幀至少停留 1 秒或以 intro/outro frame 說明；避免動畫一開始即快速跳動。
- 進行 frame-level QA：隨機/關鍵/最大值/缺資料幀檢查；自動檢查 color limits、文字、frame count 與 timestamp 序列。

### 10.3 必備動畫

| ID | 動畫 | 最低內容 |
|---|---|---|
| A01 | 表層流場與自由水面 | 固定色階、速度箭頭、AOI、UTC、缺資料 |
| A02 | SVD 主要模態重建 | mean + 代表 modes，註明僅是截斷重建 |
| A03 | TRAP 生命週期 | `s1`、curve/core/track、surface velocity、時間 |
| A04 | 三維/平面粒子逆向溯源 | 受體、粒子狀態、邊界/底部事件、scenario |
| A05 | 熱區隨季節/PC 相位變化 | normalization 固定、HDR、不確定性 |

三維動畫可用剖面/深度分面或經科學審查的 3D view；不能以透視效果犧牲深度尺度可讀性。

## 11. 必備靜態圖表與資料表

### 11.1 資料與方法

| ID | 成果 |
|---|---|
| F01 | 五站、flow domains、AOIs、focus bboxes、受體與現場作業區總圖 |
| F02 | OCM/NWW3 2024–2025 時間涵蓋/缺口圖 |
| F03 | OCM 非結構網格→共用域→1 km grid 與內插支撐示意 |
| F04 | OCM `zcor`、固定 z、HAB 與有效層示意/抽樣剖面 |
| F05 | NWW3 原生格網、mask、cycle 重疊與欄位解碼 QC |

### 11.2 SVD

| ID | 成果 |
|---|---|
| F10 | 各站平均表層/代表深度/近底流場 |
| F11 | scree/cumulative EV + bootstrap/North 判讀 |
| F12 | 主要表層向量 SVD + PC |
| F13 | 固定深度/HAB SVD 比較 |
| F14 | 2024/2025/季節模態穩定性與 subspace angle |
| F15 | PC–波/風/潮/觀測 lag/composite |

### 11.3 TRAP、粒子與熱區

| ID | 成果 |
|---|---|
| F20 | TRAP snapshot（s1、curve/core、surface velocity） |
| F21 | TRAP 頻率/強度/壽命/robustness |
| F22 | 粒子模式物理項、邊界與 scenario 示意 |
| F23 | dt/ensemble/domain/KDE 收斂 |
| F24 | 代表性逆向軌跡與三維深度演變 |
| F25 | 邊界來源 KDE/HDR 與 source AOI ranking |
| F26 | pathway/residence/coast/bottom-contact 熱區 |
| F27 | no-Stokes/finite-depth/deep/windage/Kh/Kz/浮沉敏感度 |
| F28 | SVD/PC 相位與熱區合成 |
| F29 | 粒子–TRAP 距離/接近/停留統計 |

### 11.4 驗證與綜整

| ID | 成果 |
|---|---|
| F30 | OCM–ADCP 時序/剖面/散佈/skill |
| F31 | CTD–OCM profile（資料可用時） |
| F32 | 模式熱區–聲納/ROV 廢棄物與 effort 疊圖 |
| F33 | 各站機制證據矩陣與不確定性 |
| F34 | 五站綜合來源、路徑、表層吸引與近底聚集概念圖 |

必備表格：資料涵蓋率、變數/單位契約、domain/AOI/receptor、SVD EV/穩定性、TRAP 統計、scenario matrix、粒子事件、敏感度、觀測 skill、來源 ranking、證據等級、軟體/manifest/release。

## 12. 檔名、caption 與 sidecar

檔名格式：

```text
<figure_id>_<site_or_domain>_<product>_<period>_<run_id>.<pdf|svg|png|mp4|gif>
```

每個 final artifact 同名 `.json` sidecar 至少包含：

- title、caption、alt text、figure/table ID。
- source/derived product IDs、config/run ID、Git commit。
- 時間、AOI、深度、CRS、單位、color limits、normalization。
- 使用的底圖/海岸/字型/外部素材來源與授權。
- 產生程式與命令、created_at、checksum、review status。

caption 不能只寫「結果如圖」；必須說清楚圖上量、方向、尺度、樣本數、區間與主要限制。

## 13. 結案報告大綱

1. 摘要：問題、資料、方法、主要結果、限制與管理意義。
2. 研究背景與目標：五站、海漂/海底差異、研究問題。
3. 資料：OCM/NWW3、現場觀測、涵蓋率、單位、網格與 QC。
4. 共用 flow domain 與 AOI 設計：理由、幾何、敏感度。
5. 方法：前處理、向量 SVD、TRAP、三維粒子、Stokes/SDE、熱區與驗證。
6. 結果一：平均流與 SVD 模態/時序。
7. 結果二：TRAP 空間/生命週期與表層粒子關係。
8. 結果三：逆向路徑、邊界來源、停留/底部接觸與敏感度。
9. 結果四：五站熱區與動力機制綜整、觀測驗證。
10. 討論：海漂 vs 海底、方法限制、bulk Stokes/反向擴散、資料解析度、治理含意。
11. 結論與建議：依證據等級回答 RQ，列出不可下結論事項與後續調查建議。
12. 參考資料。
13. 附錄：資料契約、參數、情境、測試、完整指標、圖/動畫索引、程式操作與 manifest。

## 14. 最終報告與 PDF QA

- 目錄、圖/表/式編號、交叉引用、DOI/URL、頁碼與附錄完整。
- 所有數值、站名、日期、深度、單位與色階跨正文/圖/caption/表一致。
- PDF 字型嵌入，中文/數學符號無黑框或缺字，線條/影像不模糊、不裁切。
- 圖中沒有 trial、placeholder、debug path、工具 token 或未審查註記。
- 逐頁渲染為 PNG 視覺檢查；零重疊、破字、孤行、表格超界與錯頁。
- 超連結、動畫索引、程式 release/checksum 可開啟；SERVER 私密路徑以資料 root token 表示。
- 報告中所有因果措辭由研究團隊對照證據矩陣審查；來源機率、表層/海底及解析度限制不得省略。
