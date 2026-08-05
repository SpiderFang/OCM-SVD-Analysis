#!/usr/bin/env python3
"""以最小 OOXML 文字替換統一既有 DOCX 的專有名詞首次定義格式。

本工具不呼叫報告產生器、不重新建立段落，也不讀取或重算任何 SVD 科學資料。它只修改
``word/document.xml`` 中明確列出的文字片段，因此使用者在 Word 內自行調整的段落、圖表、
圖片、樣式、分頁、頁碼、關係檔及其他封裝內容均原樣保留。每個目標片段必須恰好出現一次；
若文件內容已改變而無法唯一定位，工具會在寫檔前停止，避免誤改相似句子。

專有名詞採「中文全名（English Full Name, ABBR）」格式。無通用縮寫的術語則採「中文名稱
（English Term）」；首次定義之後，正文沿用縮寫或已定義名稱，不重複展開全名。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from lxml import etree


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
NAMESPACES = {"w": WORD_NAMESPACE}
DOCUMENT_XML = "word/document.xml"


@dataclass(frozen=True)
class Replacement:
    """描述一項必須唯一命中的局部文字修正。

    ``old`` 是目前使用者版文件中的完整片段，``new`` 是規範化後文字；採完整片段而非廣域
    正規表示式，是為了避免把參考文獻、圖說或其他已正確使用的縮寫一併改寫。
    """

    label: str
    old: str
    new: str


REPLACEMENTS: tuple[Replacement, ...] = (
    Replacement(
        "SVD 首次定義",
        "以多變量奇異值分解萃取",
        "以多變量奇異值分解（Singular Value Decomposition, SVD）萃取",
    ),
    Replacement(
        "摘要中的 SVD 改用縮寫",
        "建立面積加權、多變量奇異值分解（SVD）",
        "建立面積加權、多變量 SVD",
    ),
    Replacement(
        "PC 首次定義",
        "解析標準化主成分（PC）的逐時變化",
        "解析標準化主成分（Principal Component, PC）的逐時變化",
    ),
    Replacement(
        "EV 與 RMS 首次定義",
        "聯合解釋變異量、水平速度異常 RMS",
        "聯合解釋變異量（Explained Variance, EV）、水平速度異常均方根（Root Mean Square, RMS）",
    ),
    Replacement(
        "OCM 首次定義",
        "科學輸入為 OCM 表層快取",
        "科學輸入為海流模式（Ocean Current Model, OCM）表層快取",
    ),
    Replacement(
        "UTC 首次定義",
        "月份檔案合併後先依 UTC 排序",
        "月份檔案合併後先依世界協調時間（Coordinated Universal Time, UTC）排序",
    ),
    Replacement(
        "prefer-last 中文對照",
        "重複時間戳採 prefer-last 去重",
        "重複時間戳採後值優先（prefer-last）方式去重",
    ),
    Replacement(
        "Mode 首次中英對照",
        "解釋變異量與 Mode 1–5",
        "解釋變異量與模態（Mode）1–5",
    ),
    Replacement(
        "SVD 後續不重複定義",
        "奇異值分解（SVD）的作用",
        "SVD 的作用",
    ),
    Replacement(
        "RMS 後續直接使用縮寫",
        "本研究以均方根（root mean square，RMS）表示典型變動幅度",
        "本研究以 RMS 表示典型變動幅度",
    ),
    Replacement(
        "EV 後續直接使用縮寫",
        "解釋變異量（explained variance，EV）是單一模態變異量",
        "EV 是單一模態變異量",
    ),
    Replacement(
        "spatial loading 中英對照",
        "SVD 最初得到的空間 loading，可譯為『空間係數』：它是一張係數地圖",
        "SVD 最初得到的空間係數（Spatial Loading），是一張係數地圖",
    ),
    Replacement(
        "PC 後續直接使用縮寫",
        "PC（principal component，主成分時間係數）是一條無因次時間序列",
        "PC 是一條無因次時間序列",
    ),
    Replacement(
        "ddof 首次定義",
        "亦即程式中的 ddof=1",
        "亦即程式中的自由度差（Delta Degrees of Freedom, ddof）設為 1",
    ),
    Replacement(
        "North sampling error 與 bootstrap 中英對照",
        "North sampling error、年度分割或 bootstrap 檢查",
        "North 取樣誤差（North Sampling Error）、年度分割或拔靴重抽樣（Bootstrap）檢查",
    ),
    Replacement(
        "表格中的 EV 後續直接使用縮寫",
        "聯合解釋變異量（EV）",
        "聯合 EV",
    ),
    Replacement(
        "表格中的 RMS 後續直接使用縮寫",
        "速度異常均方根（RMS）",
        "速度異常 RMS",
    ),
)


def parse_args() -> argparse.Namespace:
    """解析既有 DOCX 與備份位置；工具固定採原檔就地更新。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="要局部修正的既有 DOCX")
    parser.add_argument("--backup", type=Path, required=True, help="修改前完整備份路徑")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    """以串流方式計算雜湊，供修改前後核對文件身分。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preserve_space_attribute(text_node: etree._Element) -> None:
    """在文字節點含首尾空白時保留 ``xml:space=preserve``，避免 Word 吞掉合法空格。"""

    text = text_node.text or ""
    attribute = f"{{{XML_NAMESPACE}}}space"
    if text.startswith(" ") or text.endswith(" "):
        text_node.set(attribute, "preserve")
    elif attribute in text_node.attrib:
        del text_node.attrib[attribute]


def replace_once_across_runs(root: etree._Element, replacement: Replacement) -> None:
    """跨越多個 ``w:r/w:t`` 節點替換單一片段，同時保留原 run 格式。

    Word 可能把一個句子依字型、拼字校正或使用者編輯歷程切成多個 run。直接指定單一 run
    會漏掉跨節點片段；把整段清空再重建又會破壞使用者格式。本函式先以段落可見文字定位，
    再只改動覆蓋目標字串的文字節點：新文字放在第一個節點，中間節點清空，最後節點保留
    目標字串後方內容。段落及 run 的屬性、註解錨點與其他 OOXML 元素均不變。
    """

    matches: list[tuple[etree._Element, list[etree._Element], int]] = []
    for paragraph in root.xpath("//w:body//w:p", namespaces=NAMESPACES):
        text_nodes = paragraph.xpath(".//w:t", namespaces=NAMESPACES)
        combined = "".join(node.text or "" for node in text_nodes)
        start = combined.find(replacement.old)
        while start >= 0:
            matches.append((paragraph, text_nodes, start))
            start = combined.find(replacement.old, start + len(replacement.old))

    if len(matches) != 1:
        raise ValueError(
            f"{replacement.label} 預期唯一命中 1 次，實際為 {len(matches)} 次；"
            "為保護使用者修改內容，未寫入文件。"
        )

    _, text_nodes, start = matches[0]
    end = start + len(replacement.old)
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for node in text_nodes:
        node_end = cursor + len(node.text or "")
        offsets.append((cursor, node_end))
        cursor = node_end

    first_index = next(index for index, (_, node_end) in enumerate(offsets) if node_end > start)
    last_index = next(index for index, (_, node_end) in enumerate(offsets) if node_end >= end)
    first_start, _ = offsets[first_index]
    last_start, _ = offsets[last_index]
    first_text = text_nodes[first_index].text or ""
    last_text = text_nodes[last_index].text or ""
    prefix = first_text[: start - first_start]
    suffix = last_text[end - last_start :]

    if first_index == last_index:
        text_nodes[first_index].text = prefix + replacement.new + suffix
        preserve_space_attribute(text_nodes[first_index])
        return

    text_nodes[first_index].text = prefix + replacement.new
    preserve_space_attribute(text_nodes[first_index])
    for index in range(first_index + 1, last_index):
        text_nodes[index].text = ""
        preserve_space_attribute(text_nodes[index])
    text_nodes[last_index].text = suffix
    preserve_space_attribute(text_nodes[last_index])


def patch_document_xml(source: bytes) -> bytes:
    """套用全部唯一文字修正並回傳不含 XML 宣告漂移的文件主體。"""

    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(source, parser=parser)
    for replacement in REPLACEMENTS:
        replace_once_across_runs(root, replacement)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def update_docx_in_place(input_path: Path, backup_path: Path) -> None:
    """先備份原檔，再以同目錄暫存檔原子替換 DOCX，避免中途失敗留下半成品。"""

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
    patched_document = patch_document_xml(document_entries[0])

    with tempfile.NamedTemporaryFile(
        prefix=f".{input_path.stem}_terms_",
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
    """執行局部修正並輸出備份與修改後雜湊，供後續差異及渲染稽核。"""

    args = parse_args()
    input_path = args.input.resolve()
    backup_path = args.backup.resolve()
    update_docx_in_place(input_path, backup_path)
    print(f"backup={backup_path}")
    print(f"backup_sha256={sha256_file(backup_path)}")
    print(f"updated={input_path}")
    print(f"updated_sha256={sha256_file(input_path)}")
    print(f"replacement_count={len(REPLACEMENTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
