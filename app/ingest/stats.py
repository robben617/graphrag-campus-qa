"""统计检查：导入后校验最小数据规模（对齐设计文档 §5.2 与验收 AT-01）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ..db import Database

# 最小规模阈值（对齐 §5.2）
MIN_SCALE = {
    "categories": 5,
    "entities": 30,       # 实体节点总数
    "entity_kinds": 3,    # 实体类型种数（entity/department/process）
    "relations": 40,      # 关系总数
    "cross_category": 5,  # 跨分类关系数
    "faq": 15,            # 知识问答组数
    "quiz": 20,           # 自测题数
}


@dataclass
class ScaleCheck:
    metric: str
    label: str
    actual: int
    required: int
    ok: bool


def collect(db: Database) -> Dict[str, int]:
    """返回各项统计原始计数。"""
    with db.session() as conn:
        def one(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        docs = one("SELECT COUNT(*) FROM documents")
        notices = one("SELECT COUNT(*) FROM documents WHERE doc_type='通知'")
        handbook = one("SELECT COUNT(*) FROM documents WHERE doc_type='手册'")
        clauses = one("SELECT COUNT(*) FROM clauses")
        entities = one("SELECT COUNT(*) FROM entities")
        entity_kinds = one("SELECT COUNT(DISTINCT kind) FROM entities")
        departments = one("SELECT COUNT(*) FROM entities WHERE kind='department'")
        processes = one("SELECT COUNT(*) FROM entities WHERE kind='process'")
        relations = one("SELECT COUNT(*) FROM relations")
        faq = one("SELECT COUNT(*) FROM faq")
        quiz = one("SELECT COUNT(*) FROM quiz")

        # 跨分类关系：两端点都能解析出"域"，且域不同。
        # 部门视为"职能部门"域；实体/流程取 category；文档取 category（无则"综合"）。
        ent_cat = {r["eid"]: r["category"] for r in conn.execute("SELECT eid, category FROM entities").fetchall()}
        doc_cat = {r["doc_id"]: r["category"] for r in conn.execute("SELECT doc_id, category FROM documents").fetchall()}

        def domain(nid: str) -> str:
            if nid in ent_cat:
                return ent_cat[nid] or "综合"
            if nid in doc_cat:
                return doc_cat[nid] or "综合"
            return ""

        cross = 0
        for r in conn.execute("SELECT src, dst FROM relations").fetchall():
            d1, d2 = domain(r["src"]), domain(r["dst"])
            if d1 and d2 and d1 != d2:
                cross += 1

        return {
            "documents": docs,
            "handbook": handbook,
            "notices": notices,
            "clauses": clauses,
            "categories": one("SELECT COUNT(*) FROM categories"),
            "entities": entities,
            "entity_kinds": entity_kinds,
            "departments": departments,
            "processes": processes,
            "relations": relations,
            "cross_category": cross,
            "faq": faq,
            "quiz": quiz,
        }


def check_minimum(stats: Dict[str, int]) -> Tuple[List[ScaleCheck], bool]:
    """按最小规模校验，返回明细与整体是否通过。"""
    labels = {
        "categories": "主题分类",
        "entities": "实体节点",
        "entity_kinds": "实体类型",
        "relations": "关系",
        "cross_category": "跨分类关系",
        "faq": "知识问答组",
        "quiz": "自测题",
    }
    checks: List[ScaleCheck] = []
    for metric, required in MIN_SCALE.items():
        actual = stats.get(metric, 0)
        checks.append(ScaleCheck(metric, labels[metric], actual, required, actual >= required))
    ok = all(c.ok for c in checks)
    return checks, ok


def format_report(stats: Dict[str, int], checks: List[ScaleCheck], ok: bool) -> str:
    lines = ["========== 知识库统计 =========="]
    order = [
        ("documents", "源文件总数"),
        ("handbook", "　· 手册"),
        ("notices", "　· 通知"),
        ("clauses", "条款/段落块"),
        ("categories", "主题分类"),
        ("entities", "实体节点"),
        ("entity_kinds", "实体类型种数"),
        ("departments", "　· 部门节点"),
        ("processes", "　· 流程节点"),
        ("relations", "关系"),
        ("cross_category", "跨分类关系"),
        ("faq", "FAQ 问答组"),
        ("quiz", "自测题"),
    ]
    for key, label in order:
        lines.append(f"  {label:<14}: {stats.get(key, 0)}")
    lines.append("--------------------------------")
    lines.append("最小规模校验（§5.2）：")
    for c in checks:
        mark = "✔" if c.ok else "✘"
        lines.append(f"  [{mark}] {c.label:<8} 实际 {c.actual:<4} 需 ≥ {c.required}")
    lines.append("结果：" + ("通过" if ok else "未通过（需补充数据）"))
    return "\n".join(lines)
