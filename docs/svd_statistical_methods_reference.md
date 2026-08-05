# SVD 模態穩定性與標準化主成分之方法依據

## 文件目的

本文件彙整本專案多變量海流 SVD 分析中三項方法設計的統計意義、適用範圍與文獻依據：

1. North sampling error（常稱 North rule）之相鄰模態可分離性判讀；
2. 時間 block bootstrap 之不確定性估計；
3. 標準化主成分 `pc_standardized.npy` 與回歸空間圖樣之物理解讀。

文件用於研究方法章、成果報告與程式審查時的可追溯佐證；不取代針對個別 AOI、時間窗與資料品質所做的科學判定。

## 1. North sampling error：判斷相鄰模態是否可分離

### 1.1 問題與目的

SVD／EOF 將資料的總變異分配至依序排列的模態。當兩個相鄰模態的特徵值非常接近時，有限樣本下的抽樣擾動可能使兩者的空間圖樣旋轉、排序交換或明顯改變。因此，不能僅因某模態為 SVD1 或 SVD2，便將其視為穩定且彼此獨立的物理機制。

North et al. (1982) 提出以特徵值抽樣誤差評估相鄰模態是否足夠分離。於常見的近似條件下，其特徵值不確定性量級可寫為：

$$
\delta\lambda_k \approx \lambda_k \sqrt{\frac{2}{N_{\mathrm{eff}}}},
$$

其中 \(\lambda_k\) 為第 \(k\) 個特徵值，\(N_{\mathrm{eff}}\) 為有效獨立樣本數，而非原始逐時資料筆數。若相鄰特徵值的誤差範圍重疊，該對模態應標記為可能退化（degenerate pair），並以其聯合子空間、模態組合或跨期間的一致性加以解讀。

### 1.2 對本專案的適用方式

本專案由加權資料矩陣的協方差矩陣求得特徵值，並以其回復 SVD 奇異值與解釋變異。因此，North 的 EOF 特徵值分離判讀可用於檢查本專案相鄰 SVD 模態的可分離性。建議輸出每個模態的 \(\lambda_k\)、\(\delta\lambda_k\)、相鄰模態誤差範圍重疊旗標，以及採用的 \(N_{\mathrm{eff}}\) 估計方式。

North rule 僅為有限樣本下的近似診斷，不是模態具有物理意義的證明。海流資料具潮汐、季節與事件尺度的時間相依性，故應與跨年／季節敏感度、子空間角、空間型態相關與 block bootstrap 結果共同使用。

### 1.3 報告建議文字

> 相鄰模態之可分離性依 North et al. (1982) 的特徵值抽樣誤差評估。若相鄰模態的誤差範圍重疊，結果以共同子空間呈現，不將單一模態賦予獨立且唯一的物理機制。

## 2. 時間 block bootstrap：保留流場時間相依性的不確定性估計

### 2.1 為何不採逐時獨立重抽樣

逐時海流、海面高度與潮汐訊號具有明顯時間自相關；相鄰時刻通常不是獨立觀測。若將每個小時獨立抽樣，會破壞事件持續性與潮汐／次潮結構，並傾向高估有效樣本數、低估 SVD 解釋變異與空間型態的不確定性。

Moving-block bootstrap 將連續時間片段視為重抽樣單位，以有放回方式抽取多個區塊後串接為新的時間序列。這保留區塊內的時間相依性，且同時保留每個時間片中 u、v、eta 與全部格點的空間共變異。Künsch (1989) 為此方法的基礎文獻；Wilks (1997) 進一步顯示 moving-block bootstrap 可用於同時具時間與空間相關的地球科學場。

### 2.2 建議的本專案流程

1. 以主要 PC 或代表性區域平均流速的自相關函數／積分時間尺度選擇區塊長度，並記錄診斷結果。
2. 每次重抽樣須對同一組時間索引的完整 `u`、`v`、`eta` 空間場共同取樣，避免破壞分量與格點間的協方差。
3. 在固定 AOI、格網、共同有效遮罩與前處理規則下，重新執行 SVD；每一重複樣本均計算 EV、空間型態相關、子空間角與模態配對資訊。
4. 對每一模態或可能退化的模態組，彙整 95% 區間與模態交換頻率。若頻繁交換，應報告聯合子空間而非逐一比較 SVD1、SVD2。

區塊長度不應視為固定常數：太短無法保留自相關，太長則有效區塊數不足。建議對合理的區塊長度範圍進行敏感度分析。若採用隨機區塊長度，可考慮 Politis 與 Romano (1994) 的 stationary bootstrap。

### 2.3 報告建議文字

> 鑑於逐時流場具有時間自相關，SVD 解釋變異與空間模態的不確定性以連續時間區塊進行 bootstrap 重抽樣估計，而非將逐時資料視為獨立樣本。每次重抽樣均共同保留 u、v、eta 及其空間場，以維持其時空協方差結構（Künsch, 1989；Wilks, 1997）。

## 3. `pc_standardized.npy`：標準化 PC 與回歸空間圖樣

### 3.1 數學定義

令 `pc.npy` 中第 \(k\) 個模態的原始時間係數為 \(pc_k(t)\)，共有 \(N\) 個時間樣本。程式先計算：

$$
\bar{pc}_k = \frac{1}{N}\sum_{t=1}^{N}pc_k(t),
$$

$$
s_k = \sqrt{\frac{1}{N-1}\sum_{t=1}^{N}\left(pc_k(t)-\bar{pc}_k\right)^2},
$$

並輸出：

$$
pc_k^{*}(t)=\frac{pc_k(t)-\bar{pc}_k}{s_k}.
$$

其中 \(pc_k^{*}(t)\) 即 `pc_standardized.npy`；它沒有物理單位、平均值為零，且樣本標準差為一。`ddof=1` 指分母使用 \(N-1\)，即採用樣本標準差。原始 PC 理論上因距平分解而接近零均值；實作中先移除其浮點數殘差，避免影響圖面基線。

### 3.2 為何同時輸出 `regression_*.npy`

若直接將原始 PC 除以 \(s_k\)，單模態重建的尺度會改變。因此，程式將同一尺度乘回原本具物理單位的空間 loading：

$$
\mathrm{regression\_component}_k
= \mathrm{mode\_component}_k \times s_k.
$$

故對任一物理分量（u、v 或 eta），有：

$$
\mathrm{regression\_component}_k \times pc_k^{*}(t)
\approx \mathrm{mode\_component}_k \times pc_k(t),
$$

右式為原始單模態距平重建；兩者僅相差刻意移除的極小 PC 浮點均值尾差。因而 `regression_u.npy`、`regression_v.npy` 與 `regression_eta.npy` 分別可解讀為「相應 PC 增加一個標準差時，u、v 與 eta 的局地變化」，單位分別為 m s⁻¹ per 1 standard deviation of PC 與 m per 1 standard deviation of PC。

此縮放方式是 EOF／SVD 研究常用的呈現慣例：以單位變異數 PC 搭配有物理單位的空間圖樣或回歸圖，可讓各模態時序使用相同的無因次尺度，同時維持空間場的物理可讀性。Wu 與 Straus (2004) 即以標準化 PC 配合具物理單位的 EOF／回歸圖表達每一標準差的場變化。

### 3.3 報告建議文字

> 每一模態的 PC 經中心化並以樣本標準差正規化，故 `pc_standardized` 表示無因次的模態振幅（標準差單位）。空間回歸圖樣使用相同標準差回縮放，因此表示 PC 增加一個標準差時的 u、v 或 eta 變化，且與原始 SVD loading 及 PC 的單模態重建等價。

## 4. 本專案的實作對照與限制

- `pc_standardized.npy` 與 `regression_*.npy` 已由 `surface_multivariate_svd.py` 建立；其定義、標準差與浮點均值尾差均記錄於各 run 的 `metadata.json`。
- 目前 run 的 metadata 明確指出尚未執行低通、季節分窗、年份比較或 block bootstrap；因此既有結果不可宣稱已通過 bootstrap 穩健性檢定。
- North rule 的有效樣本數必須反映時間相依性。將原始逐時樣本數直接代入，可能導致過度樂觀的可分離性結論。
- `pc_standardized` 用於視覺化與可解讀的物理尺度；`pc.npy` 則保留求解器的原始加權 SVD 時間係數。兩者不應混作不同的獨立分析結果。

## 5. 參考文獻

1. North, G. R., Bell, T. L., Cahalan, R. F., & Moeng, F. J. (1982). Sampling errors in the estimation of empirical orthogonal functions. *Monthly Weather Review, 110*(7), 699–706. https://doi.org/10.1175/1520-0493(1982)110%3C0699:SEITEO%3E2.0.CO;2
2. Künsch, H. R. (1989). The jackknife and the bootstrap for general stationary observations. *The Annals of Statistics, 17*(3), 1217–1241. https://doi.org/10.1214/aos/1176347265
3. Wilks, D. S. (1997). Resampling hypothesis tests for autocorrelated fields. *Journal of Climate, 10*(1), 65–82. https://doi.org/10.1175/1520-0442(1997)010%3C0065:RHTFAF%3E2.0.CO;2
4. Politis, D. N., & Romano, J. P. (1994). The stationary bootstrap. *Journal of the American Statistical Association, 89*(428), 1303–1313. https://doi.org/10.1080/01621459.1994.10476870
5. Hannachi, A., Jolliffe, I. T., & Stephenson, D. B. (2007). Empirical orthogonal functions and related techniques in atmospheric science: A review. *International Journal of Climatology, 27*(9), 1119–1152. https://doi.org/10.1002/joc.1499
6. Wu, Q., & Straus, D. M. (2004). AO, COWL, and observed climate trends. *Journal of Climate, 17*(11), 2139–2156. https://doi.org/10.1175/1520-0442(2004)017%3C2139:ACAOCT%3E2.0.CO;2

## 6. 補充資源

- NCAR NCL 的 [North eigenvalue-separation 實作說明](https://www.ncl.ucar.edu/Document/Functions/Contributed/eofunc_north.shtml) 採用 North et al. (1982) 的式（24），並展示特徵值誤差、上下界與分離旗標的輸出格式。
- NCAR NCL 的 [EOF 分析實例](https://www.ncl.ucar.edu/Applications/eof.shtml) 示範標準化 PC 後，以迴歸投影回原始物理場的常見流程。
