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

        # 「有文档记录但一条条款都没写进去」= 扫描件等无文本层文件，
        # 语义上等价于"该文件的内容没有进入知识库"。
        no_text_docs = int(
            conn.execute(
                "SELECT COUNT(*) FROM documents d "
                "WHERE NOT EXISTS (SELECT 1 FROM clauses c WHERE c.doc_id = d.doc_id)"
            ).fetchone()[0]
        )

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
            "no_text_documents": no_text_docs,
        }


def category_coverage(db: Database) -> List[Dict[str, Any]]:
    """各主题分类的覆盖度：文档数 / 条款数 / 实体数，以及是否有原文支撑。

    为什么会需要这个：图谱按 5 个主题分类建模了实体，但知识库里**并非每个分类
    都有原始文档**。没有原文支撑的分类，用户提问时检索必然落空、触发结构化拒答
    —— 而图谱却仍能给出归口部门，两者容易让人误判"这个分类已经完备"。
    把这个缺口显式暴露出来，比在简历/文档里假装"5 分类全部就绪"更站得住脚。
    """
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT c.name AS category,
                   c.description AS description,
                   (SELECT COUNT(*) FROM documents d WHERE d.category = c.name) AS docs,
                   (SELECT COUNT(*) FROM clauses cl JOIN documents d2 ON cl.doc_id = d2.doc_id
                     WHERE d2.category = c.name) AS clauses,
                   (SELECT COUNT(*) FROM entities e WHERE e.category = c.name) AS entities
            FROM categories c
            ORDER BY c.name
            """
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        docs = int(r["docs"])
        out.append(
            {
                "category": r["category"],
                "description": r["description"] or "",
                "documents": docs,
                "clauses": int(r["clauses"]),
                "entities": int(r["entities"]),
                "covered": docs > 0,
                "note": "" if docs > 0 else "该分类仅有图谱实体、尚无原文条款，相关提问会拒答",
            }
        )
    return out


def coverage_summary(db: Database) -> Dict[str, Any]:
    """覆盖度汇总：多少分类有原文支撑、哪些是空缺。"""
    rows = category_coverage(db)
    covered = [r["category"] for r in rows if r["covered"]]
    missing = [r["category"] for r in rows if not r["covered"]]
    return {
        "total": len(rows),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "covered": covered,
        "missing": missing,
        "detail": rows,
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
        ("no_text_documents", "未入库文件"),
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
