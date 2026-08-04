# 六區 2024–2025 固定深度流速—海面高度 SVD 執行與成果契約

## 1. 目的與成果身分

本文件定義龜山島、貢寮、新竹、後灣／海生館、北竿及南竿在 2024–2025 年全部可得資料上的固定物理深度流速—海面高度聯合 SVD 執行程序。其方法延續已發布的貢寮 2025 fixed-depth family，但六區雙年度成果為新的、不可覆寫的科學版本；不得以既有貢寮單年成果、完整表層 SVD 或其圖包替代。

每個區域的 family 有四個可比較層位：

| level | 聯合狀態向量 | 物理意義 |
| --- | --- | --- |
| `surface_reference` | `u_surface, v_surface, eta` | 僅供四層共同母體的表層參考，不取代完整表層海域成果。 |
| `z_minus_005p00m` | `u(-5 m), v(-5 m), eta` | 流速使用逐時 zcor 包夾層的線性內插；eta 仍是同時次自由水面。 |
| `z_minus_010p00m` | `u(-10 m), v(-10 m), eta` | 同上。 |
| `z_minus_020p00m` | `u(-20 m), v(-20 m), eta` | 同上；淺水區可能無法滿足上下包夾條件而被共同遮罩排除。 |

`eta` 沒有垂向層，不能描述為「-5 m eta」或由 zcor 反推。固定深度 `u/v` 僅在每時每個 source node 的 `zcor/u/v` 有限、且存在目標深度上下包夾層時內插；不進行單側、海床以下或海面以上外插。

## 2. 與表層 SVD 的強制隔離

固定深度與完整表層 SVD 的科學母體不同：前者必須取表層與全部固定深度共同可用的格點、時間交集，後者描述完整表層有效海域。因此本計畫強制使用獨立的 analysis kind、CLI、batch、結果父目錄與圖包父目錄。

```text
固定深度科學 family
  fixed_depth_svd/<analysis_label_vN>/

固定深度衍生圖包
  fixed_depth_svd_figure_bundles/<analysis_label_vN>/academic_report_ready_v8/

完整表層產品（本計畫不讀取、不修改）
  svd/<analysis_label>/
  svd_figure_bundles/<analysis_label>/<style>/
```

固定深度 batch JSON 會拒絕非 `fixed_depth_multivariate_svd` 設定，也會拒絕將結果或圖包命名空間改成表層的 `svd`、`svd_figure_bundles`。同一個輸出根目錄可以並存兩類產品，但不得由任一 CLI 覆寫另一類目錄。

## 3. 六個分析單元與分組依序執行規則

| 區域 | 分析單元 ID | 資料區域 ID | 核定狀態 | 執行組 |
| --- | --- | --- | --- | --- |
| 龜山島西側 | `guishan_surface_svd_candidate_v3` | `northeast_taiwan_common_cache_v3` | candidate | 第 1 組，與新竹同時執行 |
| 貢寮 | `gongliao_surface_svd_candidate_v3` | `northeast_taiwan_common_cache_v3` | candidate | 第 2 組，與後灣／海生館同時執行 |
| 新竹沿岸 | `hsinchu_surface_svd_candidate_v3` | `hsinchu_cache_v3` | candidate | 第 1 組，與龜山同時執行 |
| 後灣／海生館 | `houwan_nmmba_surface_svd_candidate_v3` | `houwan_nmmba_cache_v3` | candidate | 第 2 組，與貢寮同時執行 |
| 北竿 | `beigan_surface_svd_aoi_v1` | `lienchiang_common_cache_v3` | approved | 第 3 組，單獨執行 |
| 南竿 | `nangan_surface_svd_aoi_v1` | `lienchiang_common_cache_v3` | approved | 第 4 組，單獨執行 |

每個執行組最多同時執行兩個區域；每區設定兩個 I/O worker 與四個 BLAS threads。龜山／貢寮共用東北臺灣 native cache，北竿／南竿共用連江 native cache，故不允許同時執行。此安排的主要目的是降低 NFS 分散 source-node memory-map 的頁面讀取壓力，不是追求 SVD 線性代數的最大併發。

貢寮 2025 單年實測顯示，paired native/surface 讀取及垂向內插約占總耗時的 99.97%，峰值 RSS 約 15.13 GiB；雙年度首次正式 run 應先觀察實際 RSS、I/O wall time 與 swap，再決定是否維持兩區併行。若記憶體、NFS 或排程資源不足，應改為每個執行組一次只執行一區，不能改變科學設定、降低深度數或讓相同資料區域併發。

## 4. 來源資料與時間軸契約

每區讀取 2024-01 至 2025-12 共 24 個月份的 paired `ocm_native/<domain>/months/<YYYYMM>/` 與 `ocm_surface/<domain>/months/<YYYYMM>/`。每月必須符合：

- native 與 surface 均為 schema major 3、`status=ready`，且 `config_hash` 完全相同；
- paired `time_utc_ns.npy` 為一維 `int64` 並逐值完全一致；
- native `hvel(time,node,layer,component)`、`zcor(time,node,layer)` 及 surface `u/v/eta/valid(time,lat,lon)` 的時間維度與規則格網完全對齊；
- surface interpolation vertices 必須可對應 native source-node 軸，否則 fixed-depth runner 不得開始；
- `standard_partial_month` 僅在 CLI 明確使用 `--allow-partial-months` 時納入，並在 metadata 揭露；不使用 trial cache。

本計畫採 `sort_and_deduplicate_prefer_last`。所有月份套用已知 UTC 修正後，若跨月出現倒序或重複 UTC，先以 stable sort 排序；同 UTC 只保留設定年份、月份與月內索引序列中最後出現的 paired 樣本。相同保留索引必須同步套用到四層 fields、valid masks 與固定深度 bracket spans，且不得補值、重算或改變 native/surface 的數值。metadata 的 `paired_input_time_axis` 必須保存輸入／輸出時次、重排數、去重數、中位步長、最大缺口與斷點數。

## 5. 空間遮罩、缺值與 SVD

每個 level 的三變數有效條件為 `finite(u) AND finite(v) AND finite(eta)`。固定深度 family 固定採 `intersection_with_surface`：只有表層、-5、-10、-20 m 均通過 95% 時間有效率門檻，且位於核定 bbox cell-center、靜態海域遮罩內的格點，才能進入 `shared_valid_mask.npy`。各 level 僅可對不超過兩個相鄰時間樣本的內部短缺值線性插補；之後四層均完整的時次才進入 SVD。所有區域要求共同保留時次比例至少 90%。

每層沿用相同面積權重、u/v 共享向量 RMS、eta 獨立 RMS、20 個計算模態與 anchor sign convention。`vertical_bracket_span_m.npy` 是每時每格以相同水平權重內插後的上下 zcor 包夾距離，用於垂向解析度 QC；它不是模式 layer 厚度，亦不證明近底邊界層已充分解析。

## 6. SERVER 執行程序

先在目標 SERVER 設定資料與成果路徑：

```bash
export OCM_NATIVE_ROOT=/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/ocm_native
export OCM_SURFACE_ROOT=/home/mustlab/data/OCM-Preprocessed-Data/preprocessed/ocm_surface
export SVD_OUTPUT_ROOT=/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/<run-date>
```

第一步執行唯讀預檢：

```bash
./scripts/preflight_fixed_depth_svd_available_2024_2025.sh
```

只有六區均回報 `OK` 時，才執行正式的分組依序執行批次：

```bash
./scripts/run_fixed_depth_svd_batch_available_2024_2025.sh
```

同一 analysis label 已發布時，正式 batch 預設停止以保護 immutable family。確認既有成果與目前設定、來源 metadata 相容後，才可使用下列方式恢復中斷作業；它僅跳過已存在的 fixed-depth family，不能覆寫：

```bash
FIXED_DEPTH_SKIP_EXISTING=1 \
  ./scripts/run_fixed_depth_svd_batch_available_2024_2025.sh
```

所有科學 family 完成並通過驗收後，執行：

```bash
./scripts/replot_six_regions_fixed_depth_available_2024_2025.sh
```

此重繪步驟只使用已發布的 fixed-depth 陣列，產生 `academic_report_ready_v8` 圖包；不讀 native/surface cache、不重新垂向內插、不重新求解 SVD。

## 7. 成果驗收與報告限制

每個 family 必須驗證下列事項：

1. 四層 `time_utc_ns.npy`、`shared_valid_mask.npy` 與 `mean_eta.npy` 的值與 shape 完全一致。
2. `metadata.json > shared_sample_contract` 的共同格點數符合該區 minimum threshold，且 retained time fraction 至少 90%。
3. `paired_input_time_axis` 與 `source_months` 明確揭露 partial months、時間修正、最大缺口、重排與去重。
4. 各 level 的 explained variance 總和、空間正交性與 full-rank reconstruction error 通過既有數值檢查。
5. `vertical_bracket_span_m` coverage QC 顯示各深度可插值範圍；被共同遮罩排除的海域維持缺值，不得以 0 或空間外插填補。
6. candidate 區域成果必須保持 `candidate_pilot`，不得宣稱為正式保護區或核定 AOI 結論；北竿與南竿雖為核定分析區，仍需揭露資料可得性限制。

研究報告應稱為「2024–2025 全部可得 paired native/surface 樣本的固定深度 SVD」，不得稱為無缺口的完整雙年度資料。固定 z 的垂向 datum 仍須依 OCM 供應者資料契約確認；在確認前，深度標示是可重現的分析定義，不宜作超出資料契約的垂向物理解讀。
