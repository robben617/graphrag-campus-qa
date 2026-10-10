"""原始文本解析：把手册/通知文本切成"条款块"。

支持两种切分模式（对应设计文档 §3.2 的语义切分）：
- chapter：按"第X章 / 第X条"切分 —— 制度文件天然检索单元，用于手册
- paragraph：按空行分段 —— 用于无条号结构的通知/网页正文

产出块结构：{chapter, article_no, content, seq}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

CHAPTER_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零〇]+)\s*章\s*(.*)$")
ARTICLE_RE = re.compile(r"^\s*第\s*([0-9一二三四五六七八九十百零〇]+)\s*条\s*(.*)$")


@dataclass
class TextBlock:
    chapter: str = ""
    article_no: str = ""  # 如 "5"（第5条）
    content: str = ""
    seq: int = 0
    source_line: int = 0


def _clean_line(line: str) -> str:
    return line.strip().strip("\ufeff").strip()


def _normalize_chapter_title(raw: str) -> str:
    """PDF 提取的章节标题常含空格（如'总 则'），去空格、去页码杂讯后返回。"""
    title = raw.strip()
    # 去掉尾随页码/编号（如 '-1-'、'2025年学生手册'）
    title = re.sub(r"[-－—]{1,3}\s*\d+\s*[-－—]{1,3}.*$", "", title)
    title = title.replace(" ", "").replace("\u3000", "")
    return title


def split_into_blocks(text: str, mode: str = "auto") -> List[TextBlock]:
    """把整篇文本切成块。mode: chapter | paragraph | auto（有章/条结构则 chapter）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.split("\n")]
    if mode == "auto":
        mode = "chapter" if any(ARTICLE_RE.match(l) or CHAPTER_RE.match(l) for l in lines) else "paragraph"
    if mode == "paragraph":
        return _split_paragraphs(lines)
    return _split_articles(lines)


def _split_articles(lines: List[str]) -> List[TextBlock]:
    """按章/条切分：块 = 当前"第X条"标题行(去掉标题) + 后续内容，直到下一个章/条标题。"""
    blocks: List[TextBlock] = []
    cur_chapter = ""
    seq = 0
    buf: List[str] = []
    article_no = ""
    source_line = 0

    def flush() -> None:
        nonlocal buf, article_no, source_line
        content = _clean_line(" ".join(buf))
        if content or article_no:
            seq_local = seq
            blocks.append(
                TextBlock(
                    chapter=cur_chapter,
                    article_no=article_no,
                    content=content,
                    seq=seq_local,
                    source_line=source_line,
                )
            )
        buf = []
        article_no = ""
        source_line = 0

    for lineno, raw in enumerate(lines, start=1):
        line = _clean_line(raw)
        if not line:
            continue
        m_ch = CHAPTER_RE.match(line)
        if m_ch:
            flush()
            title = _normalize_chapter_title(m_ch.group(2))
            cur_chapter = title or f"第{m_ch.group(1)}章"
            continue
        m_ar = ARTICLE_RE.match(line)
        if m_ar:
            flush()
            seq += 1
            article_no = _to_arabic(m_ar.group(1))
            # 条号后的标题文字并入内容首句
            head = m_ar.group(2).strip()
            buf = [head] if head else []
            source_line = lineno
            continue
        # 普通行：若正在条内则追加；否则（章内导语）作为无条号块
        if article_no:
            buf.append(line)
        else:
            flush()
            seq += 1
            blocks.append(
                TextBlock(
                    chapter=cur_chapter,
                    article_no="",
                    content=line,
                    seq=seq,
                    source_line=lineno,
                )
            )
    flush()
    return blocks


def _split_paragraphs(lines: List[str]) -> List[TextBlock]:
    """按空行分段；跳过标题行下的空段。段内超长时按标点二次切分（每块≤600字）。"""
    blocks: List[TextBlock] = []
    seq = 0
    buf: List[str] = []

    def flush() -> None:
        nonlocal buf, seq
        para = _clean_line(" ".join(buf))
        if para:
            for piece in _split_long_paragraph(para):
                blocks.append(TextBlock(chapter="", article_no="", content=piece, seq=seq))
                seq += 1
        buf = []

    for raw in lines:
        line = _clean_line(raw)
        if not line:
            flush()
        else:
            buf.append(line)
    flush()
    return blocks


def _split_long_paragraph(para: str, limit: int = 600) -> List[str]:
    if len(para) <= limit:
        return [para]
    pieces: List[str] = []
    cur = ""
    for sentence in re.split(r"(?<=[。；!?！？])", para):
        if len(cur) + len(sentence) > limit and cur:
            pieces.append(cur)
            cur = sentence
        else:
            cur += sentence
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p.strip()]


_CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _to_arabic(token: str) -> str:
    """中文数字（1~99，含'十五'/'二十'式）与阿拉伯数字统一为阿拉伯数字字符串。"""
    if token.isdigit():
        return str(int(token))
    if token in _CN_NUM and token not in {"十"}:
        return str(_CN_NUM[token])
    if len(token) == 1 and token == "十":
        return "10"
    # 两位及以上中文数：十/二十/十五/三十五...
    result = 0
    for ch in token:
        if ch == "十":
            result = result * 10 if result else 10
        else:
            result += _CN_NUM.get(ch, 0)
    return str(result) if result else token


def read_text_file(path: str | Path) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return Path(path).read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码: {path}")
