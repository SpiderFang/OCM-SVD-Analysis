# SVD 結果圖表文獻追溯紀錄

本文件記錄 `academic_clean_postproduction_v2` 圖表設計實際查閱的學術來源，供成果報告、
論文撰寫、圖說製作與日後修改程式時逐項核對。這裡只把來源中可直接支持的畫法列為
「文獻慣例」；透明背景、移除圖中文字、資料缺口斷線等本專案需求則另外標示，避免把工程
決策錯誤歸因於原始論文。

- 查閱日期：2026-07-30
- 查閱範圍：期刊官方全文頁面、方法段落、圖說與官方 PDF
- 對應程式版本：`academic_clean_postproduction_v2`
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
| 圖內完全不含文字、色條或圖例 | 使用者指定 | 後製需求 |
| 透明 PNG、SVG、外部 `plot_metadata.json` | 無直接照搬來源 | 本專案後製與 provenance 決策 |
| 裁到有效 cell edge、排除邊界裁切箭頭 | 無直接照搬來源 | 本專案繪圖品質決策 |

## 後續引用建議

報告或論文若描述圖表方法，建議分開引用：

1. 描述 SVD 空間場與 PC 配對時，引用 Song et al. (2025)。
2. 描述流速 SVD 空間箭頭抽稀與時間模態分開呈現時，引用 de Oliveira Júnior et al. (2022)。
3. 描述標準化 PC、回歸幅度及「每一個 PC 標準差」的物理單位時，引用 Volkov et al. (2022)。
4. 「無文字透明圖層、逐日黑線、缺口斷線」應寫成本研究的製圖與品質控制設定，不應標為
   上述論文提出的方法。
