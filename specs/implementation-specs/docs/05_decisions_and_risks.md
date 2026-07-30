# 待決策與風險登錄

## 1. 使用規則

- `open`：尚未取得可稽核答案；只可使用暫定值做 trial。
- `decided`：有 owner、日期、依據、選項與影響；決策不得只留在聊天訊息。
- `superseded`：新決策取代舊版，保留歷史與受影響產品。
- 任何會改變單位、方向、時間、domain/AOI、情境、演算法或結論的決策，需更新設定/schema、受影響 product IDs、測試及報告。

本表日期以 2026 年為準。

## 2. 必須裁決的決策

| ID | 最晚日期 | 問題/owner | 暫定策略 | 未決影響與停工條件 | 狀態 |
|---|---:|---|---|---|---|
| D001 | 7/25 | SERVER 上 2024–2025 OCM/NWW3 的根路徑、權限、版本與 expected inventory；owner=資料管理者/研究團隊 | 本機樣本只作 reader fixture | 沒有根路徑可完成程式，但 DATA-001/G0 與正式兩年分析 blocked | open |
| D002 | 7/31 | OCM `hvel/w/zcor/diffusivity/wind` 單位與正方向、`wetdry_elem` 語意；owner=資料提供者/ON | 依原值保存，metadata 標 `UNCONFIRMED`，產品 quarantined | 未確認不得把速度標 m/s、不得正式 SVD/粒子/驗證 | open |
| D003 | 7/31 | NWW3 archive stem/cycle、重疊優先、`.wnd` 分量與 `.pth*` 語意；owner=資料提供者/ON | valid time 信任 member header；lexical 去重只作 trial；`.pth` 按 degree | 未確認不得正式 windage/partition 分析或 ready 去重產品 | open |
| D004 | 7/31 | flow domains、AOI polygons、focus bboxes、運算 CRS；owner=研究團隊/ON | 4 個物理 domain 群組；東北臺灣與連江採共用背景域 | 未核定可做候選/擴域 pilot，不得做正式站點統計 | open |
| D005 | 8/05 | 各站 receptor 位置、幾何、深度/HAB、位置與時間誤差；owner=研究團隊/現場團隊 | 由聲納/ROV/作業區建立 provisional receptors | 未核定不得凍結 scenario matrix | open |
| D006 | 9/05 | PDF 的「最多 1,000」與 10×20×50=10,000 矛盾；owner=研究團隊/SV/DE | 資源未確認前，以分層/LHS 的 1,000 組作預算估算，另跑完整小基準 | 未裁決可開發/pilot，不得啟動 SIM-004 全期批次 | open |
| D007 | 9/05 | 廢棄物類別、沉降/上浮、windage、Kh/Kz、沉積/再懸浮 prior；owner=研究團隊/ON | 不設單一真值；用文獻/現場可辯護的範圍做情境 | 無先驗時只能報敏感度/相對足跡，不能報來源機率 | open |
| D008 | 8/10 | ADCP/CTD、聲納/ROV 點位、分類、定位誤差與 effort 的可用性；owner=現場團隊/SV | 先建立 availability report | 無觀測時 V3/V4 降級，成因結論最多 B/C 級 | open |
| D009 | 8/10 | 儲存、CPU、記憶體、scratch、備份、ffmpeg 與執行環境；owner=DE/研究團隊 | 以完整一月 benchmark 外推，保留 30% 空間/時間緩衝 | 配額不足時依 task plan 範圍縮減，不可省 QC | open |
| D010 | 10/31 | 結案報告語言/模板、頁數、圖尺寸、引用格式、動畫交付與資料公開/授權；owner=研究團隊/機關 | 繁中正文、向量 PDF+PNG、MP4+GIF preview、DOI 連結 | 未裁決不阻止分析，但可能造成 11–12 月大量重排 | open |

## 3. 決策紀錄模板

```text
Decision ID / version:
Status / decided_at:
Owner / reviewers:
Question:
Options considered:
Decision and rationale:
Evidence / source:
Effective date:
Affected schemas/configs/products/runs:
Migration or rerun plan:
Known limitations:
Supersedes:
```

## 4. 風險矩陣

評分：可能性 L/M/H；影響 L/M/H。H/H 或 H/M 風險每週檢查。

| ID | 風險 | 可能性/影響 | 預防與緩解 | 觸發/應變 | owner |
|---|---|---|---|---|---|
| R001 | SERVER 兩年資料缺月、重複版本或 grid/schema 改變 | M/H | 先做 checksum inventory、grid/schema signature、coverage report | 任何月份不一致即分版；限制分析窗並由研究團隊核定 | DE |
| R002 | OCM 單位/濕乾語意未提供 | M/H | D002 期限、所有單位先 quarantined、不在檔名硬寫 SI | 7/31 未取得則正式 SVD/粒子延後；只交方法 trial | 研究團隊/ON |
| R003 | NWW3 cycle 去重或 `.pth/.wnd` 解讀錯誤 | H/H | 直接依 header 解碼、候選差值 audit、供應者基準案例 | 發現語意變更立即 major schema、全期波浪與粒子重跑 | DE/ON |
| R004 | 共用 flow domain 太小造成人工邊界主導來源 | M/H | 擴域 pilot、出界時間/比例與 HDR 穩定性 | 關鍵統計變化 >10% 或早期集中出界則擴域重跑 | ON |
| R005 | domain 太大造成 1 km/3D 快取與計算爆量 | H/H | common cache 不按 AOI 重複、逐月/逐變數 memmap、full-3D 先 pilot | 超過配額 70% 即停止擴產，縮減 vertical levels/可選 SVD | DE |
| R006 | 把 1 km 插值當成 1 km 真實解析度 | H/M | metadata 保存 source spacing、圖 caption 固定警示 | 若報告/簡報出現錯誤措辭，停止 release review | VR/研究團隊 |
| R007 | 固定 layer index 被誤當固定深度 | M/H | schema 只允許 surface/fixed-z/HAB，保存 surface_z/zcor 對照 | 任一正式圖只標 layer index 即退件並重建 | ON/VR |
| R008 | SVD 受潮汐、季節或 domain/mask 主導而誤解 | M/H | 向量/面積權重、濾波/季節/年份敏感度、bootstrap/subspace | 模態不穩定則以子空間/季節結果呈現，不定名單一機制 | ON/SV |
| R009 | TRAP 對梯度噪音與海岸 mask 過敏 | H/H | 公尺投影、海岸 buffer、平滑/門檻矩陣、robust catalog | 結構只在單一參數存在則標 sensitive，不進主結論 | ON/SV |
| R010 | 反向擴散被誤報為來源機率 | H/H | 正向–反向合成、條件足跡命名、prior/denominator sidecar | 未通過合成測試時禁用 probability 字樣 | SV/研究團隊 |
| R011 | bulk Stokes drift 在近岸/有限水深有大偏差 | H/H | 有限水深基準、deep/no-Stokes 敏感度、`.l`/dispersion QC | 敏感度改變 ranking 時，Stokes 不確定性升為主要結論限制 | ON |
| R012 | 海底聚集被表層 TRAP 過度推論 | M/H | 表層/近底證據矩陣、底部接觸/觀測驗證 | 無近底證據時只報表層吸引，不寫海底成因 | 研究團隊 |
| R013 | 受體/物性/到達時間組合超出計算預算 | H/M | D006、LHS/分層抽樣、pilot benchmark、checkpoint | 預估 >可用預算 120% 時按 task plan 縮減順序核定 | DE/SV |
| R014 | ADCP/CTD 與模式時間/深度不對位 | M/H | UTC、航跡、bin/HAB、平均窗、不確定性契約 | 無可靠對位的點排除並報 coverage，不強行算 RMSE | SV |
| R015 | 廢棄物調查缺 effort，假 absence 造成錯誤 skill | H/H | effort schema、presence-only 分析、空間 blocked CV | 無 effort 時禁用一般 accuracy/AUC（除非背景假設明載） | SV |
| R016 | 圖表色階、箭頭、正負號或時間不同步 | M/M | VIZ sidecar、固定 compare scale、SVD sign convention、frame QA | 自動/人工 QA 失敗不進 report release | VR |
| R017 | 程式/資料更動後舊圖仍留在報告 | M/H | product/run checksum、final asset registry、上游 superseded 影響分析 | 任一 checksum 不匹配即重建所有受影響圖/表 | VR/DE |
| R018 | 12 月才開始寫報告造成方法/圖說缺漏 | M/H | 8 月起同步撰寫、figure registry、每里程碑凍結 caption | 11/20 尚無完整大綱/圖清單則升為 critical blocker | VR/研究團隊 |
| R019 | 中文字型、數學式或 PDF 渲染破損 | M/M | 嵌入字型、向量輸出、逐頁 render QA | 黑框/缺字/裁切為 release blocker | VR |
| R020 | 私密 SERVER 路徑/資料誤納入程式 release | L/H | root token、`.gitignore`、secret/path scan、只交 manifest | 發現即停止發布、移除並重新建立乾淨 release | DE |

## 5. 風險接受與範圍縮減

風險只能由表列 owner/研究團隊接受，且必須記錄：接受理由、證據等級、影響站點/期間、報告限制文字與後續補救。不能用「時間不足」省略資料 QC、單位確認、表層/近底區分、合成測試或結果限制。

若資源確實不足，依序縮減：

1. full-3D SVD、非核心動畫、額外 predictor。
2. 情境全交叉改為分層/LHS，但保留五站、四季、主要物性與 no-Stokes/有限水深等核心敏感度。
3. 以代表月份做已核定的方法示範，並把交付名稱改為「方法與資料缺口報告」；不得冒充兩年結論。

## 6. 每週風險審查輸出

- 本週新增/關閉/升級的決策與風險。
- 超過 due date 的 open decisions 及其 blocked tasks。
- H/H、H/M 風險的指標、觸發值與趨勢。
- 已 superseded 的上游產品與需重跑的 derived runs/figures。
- 下一週研究團隊/資料提供者必須回答的問題與最小必要證據。
