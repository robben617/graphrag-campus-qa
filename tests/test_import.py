"""验收 AT-01：空库执行导入→符合最小规模；重复执行→无重复节点。"""

from __future__ import annotations

from pathlib import Path

from app.db import Database
from app.ingest.importer import run_import
from app.ingest.stats import check_minimum, collect


def _counts(db: Database) -> dict:
    with db.session() as conn:
        def one(sql):
            return conn.execute(sql).fetchone()[0]

        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "clauses": one("SELECT COUNT(*) FROM clauses"),
            "entities": one("SELECT COUNT(*) FROM entities"),
            "relations": one("SELECT COUNT(*) FROM relations"),
            "faq": one("SELECT COUNT(*) FROM faq"),
            "quiz": one("SELECT COUNT(*) FROM quiz"),
            "categories": one("SELECT COUNT(*) FROM categories"),
        }


def test_first_import_meets_minimum_scale(tmp_data_dir: Path):
    db = Database(str(tmp_data_dir / "school_kb.db"))
    run_import(db, tmp_data_dir)

    stats = collect(db)
    checks, ok = check_minimum(stats)
    assert ok, f"最小规模未达标: {[c for c in checks if not c.ok]}"

    # 关键规模数字
    assert stats["categories"] >= 5
    assert stats["entities"] >= 30
    assert stats["entity_kinds"] >= 3
    assert stats["relations"] >= 40
    assert stats["cross_category"] >= 5
    assert stats["faq"] >= 15
    assert stats["quiz"] >= 20


def test_reimport_is_idempotent(tmp_data_dir: Path):
    db = Database(str(tmp_data_dir / "school_kb.db"))
    run_import(db, tmp_data_dir)
    first = _counts(db)

    run_import(db, tmp_data_dir)
    second = _counts(db)

    assert first == second, f"两次导入计数不一致: {first} vs {second}"

    # 无重复：显式检查文档/实体/关系主键唯一（INSERT OR REPLACE 保证）
    with db.session() as conn:
        dup_doc = conn.execute(
            "SELECT doc_id, COUNT(*) c FROM documents GROUP BY doc_id HAVING c > 1"
        ).fetchall()
        dup_ent = conn.execute(
            "SELECT eid, COUNT(*) c FROM entities GROUP BY eid HAVING c > 1"
        ).fetchall()
    assert dup_doc == []
    assert dup_ent == []


def test_manifest_dangling_reference_detected(tmp_data_dir: Path):
    """关系引用悬空实体应报错而非静默入库。"""
    import json

    rel_path = tmp_data_dir / "kg" / "relations.json"
    rels = json.loads(rel_path.read_text(encoding="utf-8"))
    rels.append({"src": "ent:not_exist", "rel": "RELATED_TO", "dst": "dept:jwc"})
    rel_path.write_text(json.dumps(rels, ensure_ascii=False), encoding="utf-8")

    db = Database(str(tmp_data_dir / "school_kb.db"))
    from app.errors import ImportError_

    try:
        run_import(db, tmp_data_dir)
        assert False, "应当因悬空关系报错"
    except ImportError_ as exc:
        assert "悬空关系" in exc.message
