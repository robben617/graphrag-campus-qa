import json

from app.config import Settings
from app.db import Database
from app.retrieval.hybrid import HybridHit, RetrievalComparison
from app.service import evaluation


def test_evaluation_reports_first_relevant_clause_rank(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "evaluation.db"))
    db.init_schema()
    item = {"question": "图书能借多久？", "relevant_docs": ["library"]}
    (tmp_path / "evaluation.json").write_text(json.dumps([item]), encoding="utf-8")
    hits = [
        HybridHit(doc_id="unrelated", doc_title="其他规定", content="样例一"),
        HybridHit(doc_id="unrelated", doc_title="其他规定", content="样例二"),
        HybridHit(doc_id="library", doc_title="借阅规定", content="三十天"),
    ]
    monkeypatch.setattr(
        evaluation.hybrid_mod, "search_hybrid",
        lambda *args, **kwargs: RetrievalComparison(item["question"], [], [], hits),
    )

    report = evaluation.run_evaluation(db, Settings(data_dir=str(tmp_path), rerank_candidate_k=10))
    assert report["recall_at_k"] == 1.0
    assert report["mrr_at_k"] == 0.3333
    assert report["rerank_candidate_k"] == 10
    assert evaluation.latest_report(db)["mrr_at_k"] == report["mrr_at_k"]
