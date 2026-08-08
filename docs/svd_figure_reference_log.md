# SVD 結果圖表文獻追溯紀錄

本文件記錄 `academic_report_ready_v6`–`v9` 與水柱獨立圖面版本
`academic_report_ready_water_column_independent_v1` 實際查閱的學術來源，供成果報告、
論文撰寫、圖說製作與日後修改程式時逐項核對。這裡只把來源中可直接支持的畫法列為
「文獻慣例」；白底完整標示、資料缺口斷線等本專案需求則另外標示，避免把工程決策
錯誤歸因於原始論文。

- 查閱日期：2026-08-07
- 查閱範圍：期刊官方全文頁面、方法段落、圖說與官方 PDF
- 對應程式版本：表層 `academic_report_ready_v9`；水柱 `academic_report_ready_water_column_independent_v1`
- BibTeX：[`svd_figure_references.bib`](svd_figure_references.bib)

## 來源一：海表流 SVD 的空間向量與時間模態

### 完整書目

de Oliveira Júnior, L., Relvas, P., and Garel, E. (2022). Kinematics of surface currents at
the northern margin of the Gulf of Cádiz. *Ocean Science*, 18, 1183–1202.
https://doi.org/10.5194/os-18-1183-2022

- 官方文章頁：https://os.copernicus.org/articles/18/1183/2022/
- 官方 PDF：https://os.copernicus.org/articles/18/1183/2022/os-18-1183-2022.pdf
- 授權：Creative Commons Attribution 4.0
- 本專案主要核對位置：第 5.3 節與 Figure 7

### 實際參考內容

Figure 7 將 complex SVD 的空間模態與對應時間模態分開呈現；圖說並明載流速箭頭每三個
網格點才繪製一次，以避免向量互相遮蔽。時間模態同時呈現原變化與六個月低通結果，示範
如何在保留高頻資訊時加上一條較容易解讀的慢變化曲線。

### 套用到本專案

- 每一個模態各自輸出空間圖與 PC 時序圖，不將全年時間軸壓縮在空間圖旁。
- 流速箭頭依規則格點抽稀，且把實際 stride 寫入 `figures/plot_metadata.json`。
- PC 以逐時灰線保留原始高頻變化，另疊加逐日平均黑線協助全年尺度閱讀。

### 差異與限制

- 來源分析的是水平流速 complex SVD；本專案是 `u`、`v`、`eta` 聯合的實數 SVD，
  因此不能直接沿用來源的複數相位與振幅解讀。
- 來源使用六個月低通；本專案黑線是逐日算術平均，只是借用「原時序加平滑尺度」的視覺
  組織方式，並非重現該論文的濾波設定。

## 來源二：SVD 的標量底色、流速向量與配對 PC

### 完整書目

Song, Y., Lin, Y., Zhan, P., Liu, Z., and Cai, Z. (2025). Interannual variability of
summertime cross-isobath exchanges in the northern South China Sea: ENSO and riverine
influences. *Ocean Science*, 21, 3361–3374.
https://doi.org/10.5194/os-21-3361-2025

- 官方文章頁：https://os.copernicus.org/articles/21/3361/2025/
- 官方 PDF：https://os.copernicus.org/articles/21/3361/2025/os-21-3361-2025.pdf
- 授權：Creative Commons Attribution 4.0
- 本專案主要核對位置：SVD 方法說明與 Figure 3

### 實際參考內容

方法段落說明每個 SVD 模態都配對一條 MVPC，並把同一條 MVPC 回歸到各變數以得到可
共同解讀的空間圖。Figure 3 將標量場畫成色彩底圖、向量場疊成箭頭，例如海面高度異常
搭配地轉流；同一模態的 MVPC 則放在獨立時間序列 panel。

### 套用到本專案

- `regression_eta.npy` 作為標量底色。
- `regression_u.npy` 與 `regression_v.npy` 疊成同一模態的流速箭頭。
- 每一張空間圖只搭配同一 mode index 的標準化 PC，避免不同模態或不同時間係數混用。

### 差異與限制

- 來源包含多種大氣與海洋變數，本專案的共同狀態向量只含海表 `u`、`v`、`eta`。
- 本專案沒有複製原圖的版面、顏色、字型或標註，只採用可追溯的資料圖層組織方式。

## 來源三：標準化 PC 與具物理單位的回歸空間模態

### 完整書目

Volkov, D. L., Schmid, C., Chomiak, L., Germineaud, C., Dong, S., and Goes, M. (2022).
Interannual to decadal sea level variability in the subpolar North Atlantic: the role of
propagating signals. *Ocean Science*, 18, 1741–1762.
https://doi.org/10.5194/os-18-1741-2022

- 官方文章頁：https://os.copernicus.org/articles/18/1741/2022/
- 官方 PDF：https://os.copernicus.org/articles/18/1741/2022/os-18-1741-2022.pdf
- 授權：Creative Commons Attribution 4.0
- 本專案主要核對位置：第 3.1 節與 Figure 10

### 實際參考內容

第 3.1 節將 SVD 空間型態定義為資料場對標準化 PC 的回歸圖，因此回歸係數可解讀為 PC
改變一個標準差時的局部物理量變化。Figure 10 也以標量色彩和向量箭頭共同表達回歸場，
並分別保留壓力與風速的物理單位。

### 套用到本專案

- `pc_standardized.npy` 對每個模態移除數值均值尾差，再以樣本標準差 `ddof=1` 正規化。
- `regression_u.npy` 與 `regression_v.npy` 的單位為
  `m s-1 per 1 standard deviation of PC`。
- `regression_eta.npy` 的單位為 `m per 1 standard deviation of PC`。
- 單模態重建維持等價：
  `regression_component × standardized_PC = physical_loading × raw_PC`，只忽略 metadata
  已記錄、接近機器精度的 PC 均值尾差。

### 差異與限制

- 來源 Figure 10 顯示的是大氣壓力與風速對 PC 的回歸；本專案把同一個標準化回歸原則
  套用到海表 `eta` 與 `u/v`。
- 回歸圖表示統計共變關係及每一標準差的幅度，不能單憑圖形宣稱因果機制。

## 文獻慣例與本專案決策對照

| 本專案圖表設計 | 文獻依據 | 性質 |
|---|---|---|
| 空間模態與 PC 分開輸出 | de Oliveira Júnior et al. (2022), Fig. 7；Song et al. (2025), Fig. 3 | 文獻慣例 |
| 標量底色疊加向量箭頭 | Song et al. (2025), Fig. 3；Volkov et al. (2022), Fig. 10 | 文獻慣例 |
| 向量規則抽稀 | de Oliveira Júnior et al. (2022), Fig. 7 圖說 | 文獻慣例 |
| 標準化 PC 與每一標準差的物理回歸幅度 | Volkov et al. (2022), Sect. 3.1 | 文獻慣例 |
| 原始時間變化另疊加慢變化線 | de Oliveira Júnior et al. (2022), Fig. 7 | 借用視覺結構；時間尺度不同 |
| 逐時灰線加逐日平均黑線 | 無直接照搬來源 | 本專案全年可讀性決策 |
| 缺測時段斷線 | 無直接照搬來源 | 本專案資料完整性決策 |
| 白底完整標題、單位、色條與圖例 | 無直接照搬來源 | 本專案可解讀性與報告交付決策 |
| 標題寫完整「解釋變異量」而不只用 EV 縮寫 | 無直接照搬來源 | 本專案非專業讀者可讀性決策 |
| 經緯度軸與色條把實際上下限列為首尾刻度 | 無直接照搬來源 | 本專案數值邊界稽核決策 |
| 每張水柱速度主圖左下角直接嵌入向量參考尺；另存同 stem `_vector_scale_transparent` 全透明緊密裁切素材 | 文獻中的向量比例概念；axes-fraction quiverkey 版面參考相鄰 `OCM-NetCDF-Visualizer`、`OCM-Data-Preprocessing` | 本專案後製交付決策；主圖與透明素材均直接沿用同一 quiver scale，不編碼額外資料 |
| 暖灰向量陸地與深灰高解析海岸線疊於海洋資料場上方 | 海洋空間圖共通地理參照方式；岸線來源另依本專案圖資紀錄 | 本專案學術地圖決策 |
| 不在正式圖顯示 SVD 正負號 anchor | 無直接照搬來源 | 避免被誤認為測站的報告決策 |
| PNG、SVG 與外部 `plot_metadata.json` | 無直接照搬來源 | 本專案報告與 provenance 決策 |
| 裁到有效 cell edge、排除邊界裁切箭頭 | 無直接照搬來源 | 本專案繪圖品質決策 |

## 水柱聯合 SVD 的獨立深度圖面補充

本節針對水柱聯合狀態向量的圖面需求，補記本次提供的五項參考。核心原則是「數值上
聯合、圖面上拆分」：六層 `u/v`、唯一一次的 `eta` 與共同 PC 仍來自同一個 SVD，
但每個深度的空間圖、eta 空間圖與 PC 時序均保存為獨立檔案。這不是把資料改成六次
分層 SVD，而是把同一模態的不同物理視角拆成可後製排版的資產。

### 本次參考來源與實際套用

1. [EOF/PC 分析示意圖（ResearchGate）](https://www.researchgate.net/figure/Schematic-of-the-empirical-orthogonal-function-EOF-principal-component-PC-analysis_fig3_372347799)：作為結構示意，支持每個模態同時具有空間型態與 PC 時序的配對概念。此頁是示意圖來源，不作主要方法論或數值公式的唯一依據。
2. [Buongiorno Nardelli, Mulet, and Iudicone (2018), JGR Oceans](https://agupubs.onlinelibrary.wiley.com/doi/10.1002/2017JC013316)：提供三維海洋運動與垂向交換的物理脈絡；本專案因此保留深度資訊的獨立閱讀視角，但不把每個深度改成另一個 SVD 問題。
3. [Hong et al. (2025), Remote Sensing 17(8), 1468](https://www.mdpi.com/2072-4292/17/8/1468)：以 physical modes 與遙測資料重建三維海洋狀態；本專案沿用「同一組模態係數配對不同垂向場」的可解讀方向，並將各層輸出成獨立地圖。
4. [Constantinou and Hogg, *Intrinsic oceanic decadal variability of upper-ocean heat content*](https://arxiv.org/pdf/2012.08025)：作為海洋變異之空間型態與時間尺度分解的背景參考；本專案將 PC 時序獨立保存，避免被壓縮在多層空間 panel 中。
5. [Lee et al. (2013), *The mesoscale eddies and Kuroshio transport in the western North Pacific east of Taiwan from 8-year (2003–2010) model reanalysis*](https://link.springer.com/article/10.1007/s10236-013-0643-z)：與本案最直接相關；其結果將 EOF spatial amplitudes 與 PCs 分開呈現，並以水平流場／動力高度與垂向剖面分側檢視三維結構。本案據此把表層、10、20、30、40、50 m 六張速度圖各自交付，另交付唯一 eta 圖與同 mode PC 圖。

### 水柱圖面的強制規範

| 水柱圖面規範 | 科學意義或文獻脈絡 | 本專案交付實作 |
|---|---|---|
| 每個 mode 的六層速度各自一張 | 深度結構需能逐層比較，避免不同層在同一小圖內互相遮蔽 | `water_column_mode_XX_<level>_spatial_report` |
| eta 只出現一次 | eta 是二維自由水面高度，不具垂向層；重複六次會人為放大其權重與圖面語意 | `water_column_mode_XX_eta_spatial_report`；維度固定為 `(mode, lat, lon)` |
| PC 與空間圖以 mode index 配對 | 空間型態與時間係數是同一模態的兩個互補表示 | `water_column_mode_XX_pc_report`，不與地圖共用畫布 |
| 同一套 SVD 共享所有深度的 mode／PC | 避免把「獨立圖檔」誤解成「各深度獨立求解」 | `mode_u/mode_v` 保留 `(mode, velocity_level, lat, lon)`，共同 `pc` 保留 `(mode, time)` |
| 垂向視圖可獨立後製 | Springer 參考的水平／剖面分側呈現，與本案報告後製需求一致 | 不輸出 `mode_XX.png` 3×3 合併圖；每張速度主圖已具比例尺，另保存透明素材供必要的版面移位 |

### 引用界線

上述來源支持的是模態、時間係數、三維物理結構與水平／垂向視角的組織方式；白底、中文
標題、逐時灰線／逐日黑線、缺測斷線、固定五點色條、透明比例尺及檔名 schema 均是本專案
為報告後製與品質稽核所訂的工程交付規範。圖面不可據此單獨宣稱因果關係，也不可把一張
獨立深度圖解讀成該深度單獨解出的統計模態。

## 後續引用建議

報告或論文若描述圖表方法，建議分開引用：

1. 描述 SVD 空間場與 PC 配對時，引用 Song et al. (2025)。
2. 描述流速 SVD 空間箭頭抽稀與時間模態分開呈現時，引用 de Oliveira Júnior et al. (2022)。
3. 描述標準化 PC、回歸幅度及「每一個 PC 標準差」的物理單位時，引用 Volkov et al. (2022)。
4. 描述水柱深度結構分圖、水平／垂向視角與共同 PC 的配對時，可引用 Lee et al. (2013)，
   並同時說明本研究的「每層獨立檔案」是為報告後製所作的交付決策。
5. 「白底完整標示、逐日黑線、缺口斷線」應寫成本研究的製圖與品質控制設定，不應標為
   上述論文提出的方法。

## 岸線圖資來源

空間圖使用
`OCM-Data-Preprocessing/data/coastline/osm_land_polygons_taiwan_v1.geojson`。該檔為
OSMData land-polygons，由 OpenStreetMap `natural=coastline` 衍生，座標為 WGS84，
SHA-256 是 `9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd`。
正式報告與再散布須保留 OpenStreetMap／ODbL attribution。此圖資只用於高解析陸地
填色與海岸線，不提升 OCM 流場解析度，也不改變 SVD 遮罩或計算。
