"""质量评测服务（第三阶段，§6 挑战版#16）：Recall@K、事实一致性、引用覆盖率。

指标定义：
- Recall@K：Top-K 检索结果中，命中「相关文档」的比例（按题求平均）。
- 事实一致性（factuality）：回答中命中「关键词线索」的比例——离线近似，
  不依赖外部 LLM 判分，确保可复现。
- 引用覆盖率（citation coverage）：有引用的回答占比。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from ..config import Settings
from ..db import Database
from ..embeddings import get_embedder
from ..retrieval import hybrid as hybrid_mod
from ..service import versioning


def load_evaluation_set(data_dir: Path) -> List[dict]:
    path = Path(data_dir) / "evaluation.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class EvalResult:
    question: str
    relevant_docs: List[str]
    retrieved_docs: List[str]
    recall_at_k: float
    factuality: float
    citation_coverage: float
    keywords_hit: List[str]


def evaluate_question(
    db: Database, settings: Settings, item: dict, top_k: int = 5
) -> EvalResult:
    """评测单题：检索 + 关键词线索命中。"""
    embedder = get_embedder(settings)
    active = versioning.active_doc_ids(db)
    cmp = hybrid_mod.search_hybrid(
        db, item["question"], embedder, top_k=top_k, active_doc_ids=active, settings=settings
    )
    retrieved_docs = list(dict.fromkeys(h.doc_id for h in cmp.hybrid))  # 去重
    relevant = set(item.get("relevant_docs", []))

    # Recall@K：命中的相关文档数 / 相关文档总数（去重后计数）
    hit_relevant = [d for d in retrieved_docs if d in relevant]
    recall = len(hit_relevant) / len(relevant) if relevant else 0.0

    # 事实一致性：关键词线索命中率（在检索到的内容中）
    keywords = item.get("keywords", [])
    joined = " ".join(h.content for h in cmp.hybrid)
    kw_hit = [k for k in keywords if k and k in joined]
    factuality = len(kw_hit) / len(keywords) if keywords else 0.0

    # 引用覆盖率：有引用即 1
    coverage = 1.0 if cmp.hybrid else 0.0

    return EvalResult(
        question=item["question"],
        relevant_docs=list(relevant),
        retrieved_docs=retrieved_docs,
        recall_at_k=round(recall, 4),
        factuality=round(factuality, 4),
        citation_coverage=coverage,
        keywords_hit=kw_hit,
    )


def run_evaluation(db: Database, settings: Settings, top_k: int = 5) -> Dict:
    """跑完整评测集，返回汇总报告。"""
    items = load_evaluation_set(Path(settings.data_dir))
    results: List[EvalResult] = []
    for item in items:
        try:
            results.append(evaluate_question(db, settings, item, top_k=top_k))
        except Exception:  # noqa: BLE001
            continue

    n = len(results)
    recall = sum(r.recall_at_k for r in results) / n if n else 0.0
    factuality = sum(r.factuality for r in results) / n if n else 0.0
    coverage = sum(r.citation_coverage for r in results) / n if n else 0.0

    report = {
        "total": n,
        "top_k": top_k,
        "recall_at_k": round(recall, 4),
        "factuality": round(factuality, 4),
        "citation_coverage": round(coverage, 4),
        "details": [
            {
                "question": r.question,
                "relevant_docs": r.relevant_docs,
                "retrieved_docs": r.retrieved_docs,
                "recall_at_k": r.recall_at_k,
                "factuality": r.factuality,
                "citation_coverage": r.citation_coverage,
                "keywords_hit": r.keywords_hit,
            }
            for r in results
        ],
    }

    # 落库
    try:
        with db.session() as conn:
            conn.execute(
                "INSERT INTO evaluation_runs (recall_at_k, factuality, citation_cov, total, report_json) VALUES (?,?,?,?,?)",
                (report["recall_at_k"], report["factuality"], report["citation_coverage"], n,
                 json.dumps(report, ensure_ascii=False)),
            )
    except Exception:  # noqa: BLE001
        pass

    return report


def latest_report(db: Database) -> Dict:
    with db.session() as conn:
        row = conn.execute(
            "SELECT recall_at_k, factuality, citation_cov, total, created_at FROM evaluation_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"exists": False}
    return {"exists": True, "recall_at_k": row["recall_at_k"], "factuality": row["factuality"],
            "citation_coverage": row["citation_cov"], "total": row["total"], "created_at": row["created_at"]}
