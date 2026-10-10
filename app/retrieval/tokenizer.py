"""轻量中文关键词提取。

策略（P0 不依赖 Embedding）：
1. 优先 jieba 分词（若已安装）；
2. 过滤停用词与单字、纯标点；
3. 保留实体名/专名（见 retrieval 层做实体高亮）；
4. 兜底：jieba 未安装时用正则按连续汉字/英文/数字切分 + 常见停用词过滤。

输出仅用于关键词匹配评分，不参与语义向量检索。
"""

from __future__ import annotations

import re
from typing import List

STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没", "看", "好",
    "自己", "这", "那", "什么", "怎么", "怎样", "如何", "哪些", "请问", "一下",
    "可以", "能", "吗", "呢", "啊", "呀", "吧", "哦", "嗯", "那", "个", "与", "及",
    "或者", "还是", "关于", "对于", "以及", "多少", "几", "哪里", "为什么", "因为",
    "所以", "但是", "如果", "需要", "要求", "想", "知道", "告诉", "帮我", "请",
    "需", "要", "第", "章", "条", "款", "规定", "校园", "学校", "同学", "学生",
}

# 问题结尾语气词/常见模板词，评分时降权但不完全丢弃
_WEAK = {"什么", "怎么", "如何", "哪些", "请问", "一下", "吗", "呢"}


def _split_fallback(text: str) -> List[str]:
    """jieba 不可用时的兜底：切出连续 CJK/ASCII 片段，再按 2~6 字窗口枚举候选词。"""
    text = re.sub(r"[\s]+", " ", text)
    candidates: List[str] = []
    # 英文/数字专名（如 GPA、CET-4、12345 热线）整体保留
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9\-\./]{0,15}", text):
        candidates.append(m.group(0))
    # 连续汉字串
    for run in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        seg = run.group(0)
        candidates.append(seg)
    return candidates


def extract_keywords(question: str) -> List[str]:
    """从问句提取用于检索的关键词列表（去停用词，保留原始顺序语义弱化）。"""
    if not question:
        return []
    text = question.strip()
    words: List[str] = []
    try:
        import jieba  # noqa: WPS433

        segs = [w for w in jieba.lcut(text) if w.strip()]
        words = segs
    except ImportError:
        words = _split_fallback(text)

    result: List[str] = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w in STOPWORDS:
            continue
        # 丢弃纯标点
        if re.fullmatch(r"[\W_]+", w):
            continue
        # 单字汉字价值低（但保留英文/数字单字，如 'A'）
        if len(w) == 1 and re.fullmatch(r"[\u4e00-\u9fff]", w):
            continue
        if w not in result:
            result.append(w)
    return result


def weak_tokens(tokens: List[str]) -> List[str]:
    """返回问题中的弱语义 token（用于降权，如"什么/怎么"）。"""
    return [t for t in tokens if t in _WEAK]
