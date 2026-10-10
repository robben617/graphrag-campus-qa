"""图谱服务：节点搜索、邻居探索（1/2 跳）、分类统计、子图导出。

对齐设计文档 §6 标准版#9、验收 AT-05。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..db import Database


@dataclass
class GraphNode:
    id: str
    kind: str
    name: str
    category: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name,
                "category": self.category, "description": self.description}


@dataclass
class GraphEdge:
    src: str
    rel: str
    dst: str
    note: str = ""

    def to_dict(self) -> dict:
        return {"src": self.src, "rel": self.rel, "dst": self.dst, "note": self.note}


def _resolve_node(db: Database, node_id: str) -> Optional[GraphNode]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT eid, kind, name, category, description FROM entities WHERE eid = ?", (node_id,)
        ).fetchone()
        if row:
            return GraphNode(row["eid"], row["kind"], row["name"], row["category"], row["description"])
        # 文档节点
        row = conn.execute(
            "SELECT doc_id, title, category FROM documents WHERE doc_id = ?", (node_id,)
        ).fetchone()
        if row:
            return GraphNode(row["doc_id"], "document", row["title"], row["category"])
    return None


def search_nodes(db: Database, query: str, limit: int = 20) -> List[GraphNode]:
    """节点搜索：按名称/描述精确或子串匹配。"""
    q = query.strip()
    if not q:
        return []
    nodes: List[GraphNode] = []
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT eid, kind, name, category, description FROM entities
            WHERE name = ? OR name LIKE ? OR description LIKE ?
            LIMIT ?
            """,
            (q, f"%{q}%", f"%{q}%", limit),
        ).fetchall()
        for row in rows:
            nodes.append(GraphNode(row["eid"], row["kind"], row["name"], row["category"], row["description"]))
        # 文档标题匹配
        d_rows = conn.execute(
            "SELECT doc_id, title, category FROM documents WHERE title LIKE ? LIMIT ?", (f"%{q}%", limit)
        ).fetchall()
        for row in d_rows:
            nodes.append(GraphNode(row["doc_id"], "document", row["title"], row["category"]))
    return nodes


def _neighbors_once(db: Database, node_ids: set) -> List[GraphEdge]:
    if not node_ids:
        return []
    edges: List[GraphEdge] = []
    with db.session() as conn:
        placeholders = ",".join("?" for _ in node_ids)
        params = list(node_ids) * 2
        rows = conn.execute(
            f"""
            SELECT src, rel, dst, note FROM relations
            WHERE src IN ({placeholders}) OR dst IN ({placeholders})
            """,
            params,
        ).fetchall()
        for row in rows:
            edges.append(GraphEdge(row["src"], row["rel"], row["dst"], row["note"]))
    return edges


def neighbors(db: Database, node_id: str, depth: int = 1) -> Dict:
    """返回某节点的 K 跳邻居子图（nodes + edges）。depth ∈ {1, 2}。"""
    depth = max(1, min(2, int(depth)))
    node_ids = {node_id}
    all_edges: Dict[str, GraphEdge] = {}
    for _ in range(depth):
        edges = _neighbors_once(db, node_ids)
        for e in edges:
            all_edges[e.src + "|" + e.rel + "|" + e.dst] = e
        new_ids = set()
        for e in edges:
            new_ids.add(e.src)
            new_ids.add(e.dst)
        node_ids = node_ids | new_ids

    nodes: List[GraphNode] = []
    for nid in node_ids:
        n = _resolve_node(db, nid)
        if n:
            nodes.append(n)
    return {
        "root": node_id,
        "depth": depth,
        "nodes": [n.to_dict() for n in nodes],
        "edges": [e.to_dict() for e in all_edges.values()],
    }


def subgraph(db: Database, node_ids: Optional[List[str]] = None, depth: int = 1) -> Dict:
    """导出子图：给定若干节点（或全部实体）及其关系。"""
    if node_ids:
        roots = set(node_ids)
    else:
        roots = _all_entity_ids(db)
    result = neighbors(db, next(iter(roots)), depth) if len(roots) == 1 else _multi_root_subgraph(db, roots, depth)
    return result


def _all_entity_ids(db: Database) -> set:
    with db.session() as conn:
        rows = conn.execute("SELECT eid FROM entities").fetchall()
    return {r["eid"] for r in rows}


def _multi_root_subgraph(db: Database, roots: set, depth: int) -> Dict:
    node_ids = set(roots)
    edges: Dict[str, GraphEdge] = {}
    for _ in range(depth):
        found = _neighbors_once(db, node_ids)
        for e in found:
            edges[e.src + "|" + e.rel + "|" + e.dst] = e
        new_ids = set()
        for e in found:
            new_ids.add(e.src)
            new_ids.add(e.dst)
        node_ids |= new_ids
    nodes = [n for n in (_resolve_node(db, i) for i in node_ids) if n]
    return {
        "depth": depth,
        "nodes": [n.to_dict() for n in nodes],
        "edges": [e.to_dict() for e in edges.values()],
    }


def category_stats(db: Database) -> List[Dict]:
    """分类统计：每类实体数 + 部门/流程数。"""
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS total,
                   SUM(CASE WHEN kind='entity' THEN 1 ELSE 0 END) AS entities,
                   SUM(CASE WHEN kind='department' THEN 1 ELSE 0 END) AS departments,
                   SUM(CASE WHEN kind='process' THEN 1 ELSE 0 END) AS processes
            FROM entities WHERE category != ''
            GROUP BY category ORDER BY total DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def graph_stats(db: Database) -> Dict:
    """图谱整体统计：节点/关系计数 + 关系类型分布。"""
    with db.session() as conn:
        node_total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        rel_types = conn.execute(
            "SELECT rel, COUNT(*) AS c FROM relations GROUP BY rel ORDER BY c DESC"
        ).fetchall()
        kind_dist = conn.execute(
            "SELECT kind, COUNT(*) AS c FROM entities GROUP BY kind ORDER BY c DESC"
        ).fetchall()
    return {
        "nodes": node_total,
        "relations": rel_total,
        "relation_types": [dict(r) for r in rel_types],
        "node_kinds": [dict(r) for r in kind_dist],
    }
