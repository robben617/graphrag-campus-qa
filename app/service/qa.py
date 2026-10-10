"""GraphRAG QA 编排：提问 → 混合检索(关键词+向量+RRF) → 图遍历 → 回答/拒答 → 引用。

对齐验收：
- AT-02：相关回答 + 命中条款与来源文件
- AT-03：知识库没有 → 明确"未找到"，不编造
- AT-04：多轮指代追问（结合 ConversationService）
- AT-08：空问题/超长问题拒绝
- AT-11：旧文件被替代后依据新版本并提示
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..config import Settings
from ..db import Database
from ..embeddings import get_embedder
from ..errors import EmptyQuestionError, QuestionTooLongError, StorageUnavailable
from ..retrieval import hybrid as hybrid_mod
from ..retrieval import search as search_mod
from . import mastery as mastery_mod
from . import versioning
from .llm import LLMClient

REFUSAL_TEMPLATE = (
    "现有资料中未找到与“{question}”相关的明确规定。"
    "本系统仅依据已收录的学生手册与通知作答，不编造未收录的内容。"
    "建议你通过学校官网或相关部门（如教务处、学生工作部）进一步核实。"
)

EXTRACT_TEMPLATE = (
    "以下内容摘自知识库中与你的问题相关的条款（原文摘编，未启用大模型生成，请以学校正式文件为准）：\n\n"
)


@dataclass
class Citation:
    doc_title: str
    chapter: str = ""
    article_no: str = ""
    snippet: str = ""
    source_path: str = ""
    doc_id: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_title": self.doc_title,
            "chapter": self.chapter,
            "article_no": self.article_no,
            "snippet": self.snippet,
            "doc_id": self.doc_id,
        }


@dataclass
class Subgraph:
    nodes: List[dict] = field(default_factory=list)
    edges: List[dict] = field(default_factory=list)


@dataclass
class QAAnswer:
    question: str
    answer: str
    found: bool = True
    mode: str = "混合检索"  # 混合检索 | 大模型生成 | 精确FAQ | 拒答
    citations: List[Citation] = field(default_factory=list)
    entities: List[dict] = field(default_factory=list)
    subgraph: Subgraph = field(default_factory=Subgraph)
    degraded: bool = False
    conflict_notice: Optional[str] = None
    # 降级原因提示（如向量检索不可用 → 已切换关键词模式），对齐设计文档 §7.2
    degradation_notice: Optional[str] = None
    disclaimer: str = "本系统仅为信息查询辅助，请以学校官方发布文件为准。"

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "found": self.found,
            "mode": self.mode,
            "citations": [c.to_dict() for c in self.citations],
            "entities": self.entities,
            "subgraph": {"nodes": self.subgraph.nodes, "edges": self.subgraph.edges},
            "degraded": self.degraded,
            "conflict_notice": self.conflict_notice,
            "degradation_notice": self.degradation_notice,
            "disclaimer": self.disclaimer,
        }


def _hit_to_citation(h) -> Citation:
    snippet = h.content if len(h.content) <= 240 else h.content[:240] + "…"
    return Citation(
        doc_title=h.doc_title,
        chapter=h.chapter,
        article_no=h.article_no,
        snippet=snippet,
        source_path=getattr(h, "source_path", ""),
        doc_id=h.doc_id,
    )


class QAService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.llm = LLMClient(settings)
        self.embedder = get_embedder(settings)

    def _validate(self, question: Optional[str]) -> str:
        if question is None or not question.strip():
            raise EmptyQuestionError()
        q = question.strip()
        if len(q) > self.settings.max_question_len:
            raise QuestionTooLongError(self.settings.max_question_len)
        return q

    def _refuse(self, question: str, degradation_notice: Optional[str] = None) -> QAAnswer:
        return QAAnswer(
            question=question,
            answer=REFUSAL_TEMPLATE.format(question=question),
            found=False,
            mode="拒答",
            degraded=bool(degradation_notice),
            degradation_notice=degradation_notice,
        )

    def ask(self, question: Optional[str], history: Optional[List[dict]] = None) -> QAAnswer:
        q = self._validate(question)

        try:
            if self.db.is_empty():
                return QAAnswer(
                    question=q,
                    answer="知识库为空：尚未导入数据。请先执行 python -m app.cli import 完成数据导入后再提问。",
                    found=False,
                    mode="拒答",
                )
        except StorageUnavailable:
            raise

        # 时效过滤：仅检索现行有效且已生效、未被替代的文档
        active_ids = versioning.active_doc_ids(self.db)

        # 混合检索（关键词 + 向量 + RRF + 可选 reranker）
        # 向量路不可用时 search_hybrid 内部已降级为关键词单路，这里只负责把提示带出去
        comparison = hybrid_mod.search_hybrid(
            self.db, q, self.embedder, top_k=self.settings.top_k,
            active_doc_ids=active_ids, settings=self.settings,
        )
        hits = comparison.hybrid
        degradation = comparison.degraded_reason or None

        # FAQ 精确命中 → 确定性回答（但引用仍走检索）
        faq_match = search_mod.search_faq(self.db, q)

        # 相关实体（用于图遍历与答案标注）
        entities = self._related_entities(q)

        # 冲突检测（命中文档是否含被替代版本）
        conflict = None
        if hits:
            conflict = versioning.check_conflict(self.db, [h.doc_id for h in hits])

        # 拒答判定以「关键词检索是否命中实质内容」为准：
        # 离线哈希向量器对中文区分度有限，会把无关条款也召回（见 diag），
        # 因此不能仅凭向量命中就认定"知识库有资料"。关键词零命中且无 FAQ → 拒答。
        if not comparison.keyword and not faq_match:
            return self._refuse(q, degradation)

        if not hits and not faq_match:
            return self._refuse(q, degradation)

        # 优先 FAQ 确定性回答
        if faq_match:
            return self._faq_answer(q, faq_match, hits, entities, active_ids, degradation)

        # 生成式/摘编回答
        citations = [_hit_to_citation(h) for h in hits]
        evidence = [
            {"doc_title": h.doc_title, "chapter": h.chapter, "article_no": h.article_no, "content": h.content}
            for h in hits
        ]
        answer_text, mode, degraded = self._generate(q, evidence, hits)

        # 掌握标记提示：未掌握的相关知识点追加提示
        answer_text = self._append_mastery_hint(answer_text, entities)

        return QAAnswer(
            question=q,
            answer=answer_text,
            found=True,
            mode=mode,
            citations=citations,
            entities=entities,
            subgraph=self._build_subgraph(entities),
            # 两个降级原因任一成立都算降级：向量检索不可用 / LLM 生成失败
            degraded=degraded or bool(degradation),
            conflict_notice=conflict,
            degradation_notice=degradation,
        )

    def _faq_answer(self, q, faq_match, hits, entities, active_ids, degradation_notice=None) -> QAAnswer:
        citations = [_hit_to_citation(h) for h in hits]
        answer = faq_match["answer"]
        answer = self._append_mastery_hint(answer, entities)
        return QAAnswer(
            question=q,
            answer=answer,
            found=True,
            mode="精确FAQ",
            citations=citations,
            entities=entities,
            subgraph=self._build_subgraph(entities),
            degraded=bool(degradation_notice),
            degradation_notice=degradation_notice,
        )

    def _generate(self, q, evidence, hits):
        try:
            answer_text = self.llm.generate(q, evidence)
            return answer_text, "大模型生成", False
        except Exception:  # noqa: BLE001 - 任意异常都降级
            return self._extract_answer(q, hits), "混合检索", True

    def _extract_answer(self, question, hits) -> str:
        parts = [EXTRACT_TEMPLATE.rstrip("\n")]
        for i, h in enumerate(hits, 1):
            parts.append(f"〔来源{i}〕{h.citation()}")
            parts.append(h.content.strip())
            parts.append("")
        return "\n".join(parts).rstrip("\n")

    def _related_entities(self, question: str) -> List[dict]:
        from ..retrieval import tokenizer

        tokens = tokenizer.extract_keywords(question)
        ents = search_mod.search_entities(self.db, tokens)
        out = []
        seen = set()
        for e in ents:
            if e["name"] not in seen:
                seen.add(e["name"])
                out.append(
                    {"id": e["eid"], "name": e["name"], "kind": e["kind"], "category": e["category"]}
                )
        return out

    def _build_subgraph(self, entities: List[dict]) -> Subgraph:
        if not entities:
            return Subgraph()
        from . import graph as graph_mod

        ids = [e["id"] for e in entities if e.get("id")]
        if not ids:
            return Subgraph()
        sg = graph_mod.subgraph(self.db, node_ids=ids, depth=1)
        return Subgraph(nodes=sg.get("nodes", []), edges=sg.get("edges", []))

    def _append_mastery_hint(self, answer: str, entities: List[dict]) -> str:
        unmastered = set(mastery_mod.unmastered_names(self.db))
        if not unmastered:
            return answer
        related_unmastered = [e["name"] for e in entities if e["name"] in unmastered]
        if not related_unmastered:
            return answer
        hint = "\n\n【学习提示】以下相关知识点你标记为未掌握，建议重点复习：" + "、".join(related_unmastered) + "。"
        return answer + hint
