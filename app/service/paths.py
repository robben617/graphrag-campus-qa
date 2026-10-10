"""关系路径服务：为命中的相关实体展开「实体 → 归口部门/流程」的关系路径。

第三阶段 §6 挑战版#14「答案引用：来源条款、关系路径、原文片段全量可展开」中的
「关系路径」部分：从实体出发沿关系边走到部门/流程，形成可解释的路径。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..db import Database


def relation_paths(db: Database, entity_id: str, max_depth: int = 2) -> List[List[dict]]:
    """返回从 entity 出发、长度 ≤ max_depth 的关系路径（BFS 到部门/流程即停）。

    每条路径为 [ {from, rel, to, note}, ... ]。
    """
    if max_depth <= 0:
        return []
    with db.session() as conn:
        # 记录每个节点的 kind（entity/department/process/document）
        def kind_of(nid: str) -> str:
            row = conn.execute("SELECT kind FROM entities WHERE eid = ?", (nid,)).fetchone()
            if row:
                return row["kind"]
            row = conn.execute("SELECT doc_id FROM documents WHERE doc_id = ?", (nid,)).fetchone()
            return "document" if row else "unknown"

        paths: List[List[dict]] = []
        frontier: List[List[dict]] = [[{"from": entity_id, "rel": None, "to": entity_id, "note": ""}]]
        visited = {entity_id}

        for depth in range(max_depth):
            nxt: List[List[dict]] = []
            for path in frontier:
                cur = path[-1]["to"]
                if kind_of(cur) in {"department", "process", "document"} and depth > 0:
                    paths.append(path)
                    continue
                rows = conn.execute(
                    "SELECT src, rel, dst, note FROM relations WHERE src = ? OR dst = ?",
                    (cur, cur),
                ).fetchall()
                for r in rows:
                    nbr = r["dst"] if r["src"] == cur else r["src"]
                    if nbr in visited:
                        continue
                    rel = r["rel"]
                    # 有向：src--rel-->dst
                    if r["src"] == cur:
                        step = {"from": cur, "rel": rel, "to": nbr, "note": r["note"]}
                    else:
                        step = {"from": nbr, "rel": rel, "to": cur, "note": r["note"]}
                    new_path = path + [step]
                    nxt.append(new_path)
                    visited.add(nbr)
            frontier = nxt
            if not frontier:
                break
        # 收集到终点的路径 + 未走到终点的也保留（去掉首节点自环占位）
        for p in frontier:
            if len(p) > 1:
                paths.append(p)
        return paths
