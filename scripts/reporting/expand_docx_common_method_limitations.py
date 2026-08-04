#!/usr/bin/env python3
"""局部擴寫既有聚焦版 DOCX 的「10.7 共用方法限制」。

本工具專供已由研究人員在 Word 內人工修訂的正式文件使用。它不呼叫整份報告產生器，
也不重算 SVD 或重建圖表；唯一科學內容變更是把 10.7 原有五個簡短條列，改寫為七個
「限制來源、可能影響、正確解讀方式」均完整交代的條列。其餘段落、圖片、樣式、分頁
設定、頁首頁尾、關係檔與封裝內容均沿用來源檔。

為防止套用到錯誤版本，標題與五個舊條列必須逐字且唯一命中。工具會先建立完整備份，
再透過同目錄暫存檔原子替換來源 DOCX；任何定位或封裝檢查失敗都會在覆寫前停止。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from lxml import etree


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
NAMESPACES = {"w": WORD_NAMESPACE}
DOCUMENT_XML = "word/document.xml"
SECTION_HEADING = "10.7　共用方法限制"
NEXT_HEADING = "參考文獻"

# 舊文字固定對應目前人工修訂版，可避免在內容已變更時錯誤覆寫研究人員的新修改。
OLD_LIMITATIONS: tuple[str, ...] = (
    "PC 時序只使用來源資料實際提供且通過共同有效性檢查的時間步；圖中的空白區段保留資料缺口，未推估缺失狀態。",
    "模態排序與空間型態依分析邊界、共同有效遮罩、U₀ 與 E₀ 的定義而定；邊界或尺度改變時應建立新版分析單元並完整重算。",
    "SVD 為線性正交基底，單一物理過程可能分布於相鄰模態，傳播或旋轉訊號亦可能以成對模態表示。",
    "速度異常 RMS 與方向同向性為區域尺度指標；近岸邊界、局地渦旋及來源位置可能使實際漂移方向偏離區域平均向量。",
    "分析期間限定為 2024–2025 年；相近特徵值模態宜以 North 取樣誤差（North Sampling Error）、年度分割或拔靴重抽樣（Bootstrap）檢查可分離性與時序穩健性。",
)

# 每項均明示「資料或方法為何產生此限制」、「結果可能怎樣受影響」與「不得如何過度解讀」。
NEW_LIMITATIONS: tuple[str, ...] = (
    "本分析萃取的是 OCM 所提供之表層 u、v 與 η 的主要共同變化，而不是新增的現地觀測。SVD 會忠實整理輸入資料中的訊號，也會一併承接模式解析度、海岸線表現及原始場可能存在的系統偏差。因此，模態可解讀為「模式資料中最穩定且最常重複的流場結構」，不宜逕行延伸為研究海域所有真實細尺度流況的完整描述。",
    "PC 時序只使用來源資料實際提供且通過共同有效性檢查的時間步。資料缺口保留為空白，不以跨缺口插值或零值補齊，目的是避免人為製造平滑訊號；相對地，SVD 統計結果代表的是「可用時段」內的變化。若缺口恰與颱風、強流或其他極端海況重疊，該事件對模態與 EV 的貢獻可能被低估，因此空白區段不能解讀為流場平靜或 PC 等於零。",
    "模態排序與空間型態不是脫離分析設定的固定答案，而會受局部研究範圍、共同有效遮罩及 U₀、E₀ 尺度定義影響。改變邊界會增減納入的海岸、外海或水道格點；改變遮罩會改變可共同計算的樣本；改變 U₀ 與 E₀ 則會調整速度與海表面高度在聯合矩陣中的相對權重。上述設定任一變更，都可能使 EV、模態排序與空間型態改變，故不同版本不能只依 Mode 編號直接對照，必須視為新的分析單元重新計算並記錄版本。",
    "EV 衡量的是經尺度正規化後 u、v 與 η 的聯合變異，不是純水平流速的占比。某模態可能因 η 訊號強而具有較高 EV，但速度異常 RMS 不一定同步較大；反之，EV 次高的模態仍可能具有更清楚且一致的水平速度結構。因此，本報告以 EV、速度異常 RMS、方向同向性及背景平均流共同判讀候選模態，不能只憑 EV 排名宣稱其必然最主導海廢的水平輸送。",
    "SVD 要求模態在數學上彼此正交，目的在避免重複計算同一部分變異；然而，真實海洋過程不一定彼此正交，也可能具有非線性、位相移動或隨時間改變的空間結構。一個傳播、旋轉或位置逐漸移動的訊號，常會被拆成相鄰兩個模態共同表示；若兩個模態的 EV 接近，個別空間圖也可能因抽樣差異而產生旋轉或互換。因此，不宜把單一 Mode 直接等同於唯一物理機制，應同時閱讀相鄰模態的空間圖與 PC 時序。",
    "速度異常 RMS 表示整個研究範圍內，每 1 個標準差 PC 所對應的典型流速改變幅度；方向同向性則衡量各格點箭頭是否大致朝向一致。兩者都是區域彙整指標：高 RMS 可能集中在少數格點，高同向性也不代表每個近岸位置皆具有相同流向。海岸形狀、局地渦旋、剪切帶及海廢所在起始位置，都可能使局部漂移方向偏離區域平均向量；因此，這些指標適合用於篩選候選模態，不宜直接推定特定來源點、近岸滯留區或到達位置。",
    "分析期間僅涵蓋 2024–2025 年，季節循環只有兩次完整重複，對少見極端事件與較長年際變化的代表性有限。North 取樣誤差、年度分割與拔靴重抽樣可用來檢查相近特徵值是否可分離，以及模態型態是否對抽樣敏感；但這些檢查只能量化本資料期間內的穩健性，不能補足未涵蓋的年份。若相鄰模態的差異小於取樣不確定性，較嚴謹的解讀是將其視為共同子空間或成對結構，而不是堅持固定的 Mode 先後次序。",
)


def parse_args() -> argparse.Namespace:
    """解析正式 DOCX、備份位置與經渲染確認後的參考文獻頁碼。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="要局部修正的既有 DOCX")
    parser.add_argument("--backup", type=Path, required=True, help="修改前完整備份路徑")
    parser.add_argument(
        "--reference-page",
        type=int,
        help="若擴寫造成分頁位移，將靜態目錄的參考文獻頁碼更新為此值",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    """以串流方式計算檔案雜湊，供修改前後及備份完整性核對。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paragraph_text(paragraph: etree._Element) -> str:
    """合併段落內所有 Word 文字節點，跨 run 還原使用者可見文字。"""

    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NAMESPACES))


def preserve_space_attribute(text_node: etree._Element) -> None:
    """在文字含首尾空白時保留 ``xml:space``，避免 Word 自動吞掉合法空格。"""

    text = text_node.text or ""
    attribute = f"{{{XML_NAMESPACE}}}space"
    if text.startswith(" ") or text.endswith(" "):
        text_node.set(attribute, "preserve")
    elif attribute in text_node.attrib:
        del text_node.attrib[attribute]


def set_paragraph_text(paragraph: etree._Element, expected: str, replacement: str) -> None:
    """只改既有文字節點內容，保留段落、條列、字型與人工直接格式。

    來源句子可能因 Word 編輯歷程被切成多個 run。新句完整放入第一個文字節點，其餘文字
    節點清空；run 屬性與非文字 OOXML 元素仍保留，因此不會重建或重設條列樣式。
    """

    if paragraph_text(paragraph) != expected:
        raise ValueError(f"10.7 條列文字與預期版本不符：{paragraph_text(paragraph)!r}")
    text_nodes = paragraph.xpath(".//w:t", namespaces=NAMESPACES)
    if not text_nodes:
        raise ValueError("10.7 條列沒有可寫入的 w:t 文字節點")
    text_nodes[0].text = replacement
    preserve_space_attribute(text_nodes[0])
    for text_node in text_nodes[1:]:
        text_node.text = ""
        preserve_space_attribute(text_node)


def remove_internal_page_breaks(paragraph: etree._Element) -> None:
    """移除 10.7 條列內既有的手動分頁符號，由參考文獻標題控制獨立分頁。

    舊版最後一個條列尾端含一個 ``w:br type=page``，原本用來把參考文獻推到下一頁。
    擴寫後若直接複製該段落，分頁符號會被複製到每個新增條列，造成一項限制占一整頁。
    參考文獻標題本身已設定分頁前，故在 10.7 範圍內移除此冗餘符號不會破壞獨立頁要求。
    """

    page_breaks = paragraph.xpath(".//w:br[@w:type='page']", namespaces=NAMESPACES)
    for page_break in page_breaks:
        page_break.getparent().remove(page_break)


def append_reference_page_break(paragraph: etree._Element) -> None:
    """在最後一個限制條列尾端加入唯一手動分頁，確保參考文獻維持獨立頁。

    使用額外空白 run 承載分頁，不修改條列文字或字型。此位置與舊版的分頁意圖相同，
    但只放在新七項限制的最後一項，避免複製模板時產生多個空白頁。
    """

    run = etree.SubElement(paragraph, f"{{{WORD_NAMESPACE}}}r")
    page_break = etree.SubElement(run, f"{{{WORD_NAMESPACE}}}br")
    page_break.set(f"{{{WORD_NAMESPACE}}}type", "page")


def update_reference_toc_page(root: etree._Element, page_number: int) -> None:
    """更新靜態目錄中「參考文獻」那一列的頁碼，不碰正文標題或其他數字。

    目錄採兩欄表格而非 Word 自動目錄；因此擴寫後必須依實際渲染頁面回填。函式先以
    第一欄文字唯一定位表格列，再要求第二欄僅含一個整數，避免誤改正文或書目年份。
    """

    rows = []
    for row in root.xpath("//w:tbl/w:tr", namespaces=NAMESPACES):
        cells = row.xpath("./w:tc", namespaces=NAMESPACES)
        if len(cells) != 2:
            continue
        first = "".join(cells[0].xpath(".//w:t/text()", namespaces=NAMESPACES)).strip()
        if first == NEXT_HEADING:
            rows.append(cells)
    if len(rows) != 1:
        raise ValueError(f"靜態目錄參考文獻列預期 1 列，實際為 {len(rows)} 列")

    page_nodes = rows[0][1].xpath(".//w:t", namespaces=NAMESPACES)
    old_page = "".join(node.text or "" for node in page_nodes).strip()
    if not old_page.isdigit() or not page_nodes:
        raise ValueError(f"靜態目錄參考文獻頁碼不是唯一整數：{old_page!r}")
    page_nodes[0].text = str(page_number)
    for node in page_nodes[1:]:
        node.text = ""


def patch_document_xml(source: bytes, reference_page: int | None) -> bytes:
    """定位 10.7、擴寫七個條列，並視需要回填已驗證的參考文獻頁碼。"""

    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(source, parser=parser)
    body_paragraphs = root.xpath("//w:body/w:p", namespaces=NAMESPACES)
    headings = [paragraph for paragraph in body_paragraphs if paragraph_text(paragraph) == SECTION_HEADING]
    if len(headings) != 1:
        raise ValueError(f"10.7 標題預期唯一命中 1 次，實際為 {len(headings)} 次")

    heading_index = body_paragraphs.index(headings[0])
    following = body_paragraphs[heading_index + 1 :]
    next_heading_positions = [index for index, p in enumerate(following) if paragraph_text(p) == NEXT_HEADING]
    if not next_heading_positions:
        raise ValueError("10.7 後找不到參考文獻標題，無法確認修改邊界")
    limitation_paragraphs = following[: next_heading_positions[0]]
    if tuple(paragraph_text(p) for p in limitation_paragraphs) != OLD_LIMITATIONS:
        raise ValueError("10.7 現有五個條列與鎖定版本不符；為保護人工修改，未寫入文件")

    # 必須在改寫前保存條列模板；否則第五段完成改寫後，其文字已不是鎖定的舊句，會使
    # 新增段落的版本防護檢查誤判。模板只存在記憶體內，仍完整保留原始 ListBullet 格式。
    bullet_template = deepcopy(limitation_paragraphs[-1])

    # 前五段沿用原段落物件；新增兩段則複製同一條列段落，保留 ListBullet、編號與直接格式。
    for paragraph, old_text, new_text in zip(
        limitation_paragraphs,
        OLD_LIMITATIONS,
        NEW_LIMITATIONS[: len(OLD_LIMITATIONS)],
        strict=True,
    ):
        set_paragraph_text(paragraph, old_text, new_text)
        remove_internal_page_breaks(paragraph)

    insertion_anchor = limitation_paragraphs[-1]
    for new_text in NEW_LIMITATIONS[len(OLD_LIMITATIONS) :]:
        new_paragraph = deepcopy(bullet_template)
        set_paragraph_text(new_paragraph, OLD_LIMITATIONS[-1], new_text)
        remove_internal_page_breaks(new_paragraph)
        insertion_anchor.addnext(new_paragraph)
        insertion_anchor = new_paragraph

    # 參考文獻必須獨立一頁；10.7 內僅在最後一項保留一個分頁控制。
    append_reference_page_break(insertion_anchor)

    if reference_page is not None:
        update_reference_toc_page(root, reference_page)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def update_docx_in_place(input_path: Path, backup_path: Path, reference_page: int | None) -> None:
    """完整備份來源檔後，只替換 ``document.xml``，並以原子操作完成就地更新。"""

    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        raise FileExistsError(f"備份檔已存在，為避免覆寫而停止：{backup_path}")
    shutil.copy2(input_path, backup_path)

    with ZipFile(input_path, "r") as source_zip:
        entries = [(info, source_zip.read(info.filename)) for info in source_zip.infolist()]
    document_entries = [data for info, data in entries if info.filename == DOCUMENT_XML]
    if len(document_entries) != 1:
        raise ValueError(f"DOCX 中找不到唯一的 {DOCUMENT_XML}")
    patched_document = patch_document_xml(document_entries[0], reference_page)

    with tempfile.NamedTemporaryFile(
        prefix=f".{input_path.stem}_limitations_",
        suffix=".docx",
        dir=input_path.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(temporary_path, "w") as target_zip:
            for info, data in entries:
                target_zip.writestr(info, patched_document if info.filename == DOCUMENT_XML else data)
        temporary_path.replace(input_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    """執行局部擴寫，輸出路徑、雜湊與條列數供後續差異及渲染稽核。"""

    args = parse_args()
    input_path = args.input.resolve()
    backup_path = args.backup.resolve()
    update_docx_in_place(input_path, backup_path, args.reference_page)
    print(f"backup={backup_path}")
    print(f"backup_sha256={sha256_file(backup_path)}")
    print(f"updated={input_path}")
    print(f"updated_sha256={sha256_file(input_path)}")
    print(f"limitation_count={len(NEW_LIMITATIONS)}")
    if args.reference_page is not None:
        print(f"reference_page={args.reference_page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
