from app.db import Database
from app.service import ingestion, versioning


def test_archived_notice_cannot_replace_newer_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(versioning, "_today", lambda: "2026-08-01")
    db = Database(str(tmp_path / "notices.db"))
    db.init_schema()
    new = ingestion.ingest_notice(
        db, "六月十日截止。", title="2026年奖学金评定通知", category="资助", effective_date="2026-06-01"
    )
    old = ingestion.ingest_notice(
        db, "六月一日截止。", title="2025年奖学金评定通知", category="资助", effective_date="2025-06-01"
    )

    assert not old["superseded"]
    assert old["archived_by"] == [new["doc_id"]]
    assert versioning.superseded_by(db, old["doc_id"]) == new["doc_id"]
    assert versioning.active_doc_ids(db) == {new["doc_id"]}


def test_scheduled_successor_preserves_current_notice_until_effective(tmp_path, monkeypatch):
    monkeypatch.setattr(versioning, "_today", lambda: "2026-08-01")
    db = Database(str(tmp_path / "notices.db"))
    db.init_schema()
    old = ingestion.ingest_notice(
        db, "借阅期限十四天。", title="2025年图书借阅规定", category="图书馆", effective_date="2025-06-01"
    )
    pending = ingestion.ingest_notice(
        db, "借阅期限三十天。", title="2026年图书借阅规定", category="图书馆", effective_date="2026-09-01"
    )
    assert pending["scheduled_supersedes"] == [old["doc_id"]]
    assert not pending["superseded"]
    assert versioning.active_doc_ids(db) == {old["doc_id"]}
    assert versioning.superseded_by(db, old["doc_id"]) is None

    # 早期版本写库时会提前修改旧版状态，查询仍按生效日判断。
    with db.session() as conn:
        conn.execute("UPDATE documents SET status = '已被替代' WHERE doc_id = ?", (old["doc_id"],))
    assert versioning.active_doc_ids(db) == {old["doc_id"]}

    monkeypatch.setattr(versioning, "_today", lambda: "2026-09-01")
    assert versioning.active_doc_ids(db) == {pending["doc_id"]}
    assert versioning.superseded_by(db, old["doc_id"]) == pending["doc_id"]
