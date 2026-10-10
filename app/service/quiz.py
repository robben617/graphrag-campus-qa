"""自测题库服务：抽题（按分类/难度）、判题给解析、错题复习、不重复出题。

对齐设计文档 §6 标准版#10、验收 AT-06。
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..db import Database


def list_categories(db: Database) -> List[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS total, SUM(CASE WHEN difficulty='简单' THEN 1 ELSE 0 END) AS easy, "
            "SUM(CASE WHEN difficulty='中等' THEN 1 ELSE 0 END) AS medium, "
            "SUM(CASE WHEN difficulty='困难' THEN 1 ELSE 0 END) AS hard "
            "FROM quiz GROUP BY category ORDER BY category"
        ).fetchall()
    return [dict(r) for r in rows]


def draw_questions(
    db: Database,
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
    count: int = 5,
    avoid_answered: bool = True,
    user_id: Optional[str] = None,
) -> List[dict]:
    """抽题。avoid_answered=True 时优先抽未答过的题（不重复出题）。"""
    where, params = [], []
    if category:
        where.append("category = ?")
        params.append(category)
    if difficulty:
        where.append("difficulty = ?")
        params.append(difficulty)
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    answered_qids: set = set()
    if avoid_answered:
        with db.session() as conn:
            if user_id:
                answered_qids = {r[0] for r in conn.execute(
                    "SELECT DISTINCT qid FROM quiz_attempts WHERE user_id = ?", (user_id,)
                ).fetchall()}
            else:
                answered_qids = {r[0] for r in conn.execute("SELECT DISTINCT qid FROM quiz_attempts").fetchall()}

    with db.session() as conn:
        rows = conn.execute(f"SELECT * FROM quiz{cond} ORDER BY qid", params).fetchall()

    items = [dict(r) for r in rows]
    unanswered = [q for q in items if q["qid"] not in answered_qids]
    answered = [q for q in items if q["qid"] in answered_qids]

    picked = unanswered[:count]
    if len(picked) < count:
        picked += answered[: count - len(picked)]

    out = []
    for q in picked:
        options = json.loads(q["options"]) if q["options"] else []
        out.append(
            {
                "qid": q["qid"],
                "category": q["category"],
                "difficulty": q["difficulty"],
                "question": q["question"],
                "options": options,
                # 判题前不下发答案
            }
        )
    return out


def submit_answer(db: Database, qid: str, chosen: int, user_id: str = "anonymous") -> dict:
    """判题：返回是否正确、正确答案下标、解析。"""
    with db.session() as conn:
        row = conn.execute(
            "SELECT qid, question, options, answer, explanation, source, category FROM quiz WHERE qid = ?", (qid,)
        ).fetchone()
    if not row:
        from ..errors import NotFoundError

        raise NotFoundError("题目不存在", detail=qid)
    correct_idx = int(row["answer"])
    is_correct = int(chosen) == correct_idx
    with db.session() as conn:
        conn.execute(
            "INSERT INTO quiz_attempts (qid, user_id, chosen, correct) VALUES (?,?,?,?)",
            (qid, user_id, chosen, 1 if is_correct else 0),
        )
    options = json.loads(row["options"]) if row["options"] else []
    return {
        "qid": qid,
        "question": row["question"],
        "options": options,
        "chosen": chosen,
        "correct_index": correct_idx,
        "is_correct": is_correct,
        "explanation": row["explanation"],
        "source": row["source"],
        "category": row["category"],
    }


def wrong_answers(db: Database, user_id: Optional[str] = None) -> List[dict]:
    """错题集：最近一次作答错误的题。"""
    with db.session() as conn:
        if user_id:
            rows = conn.execute(
                """
                SELECT a.qid, a.chosen, a.correct, a.created_at, q.question, q.options, q.answer,
                       q.explanation, q.source, q.category, q.difficulty
                FROM quiz_attempts a JOIN quiz q ON q.qid = a.qid
                WHERE a.correct = 0 AND a.user_id = ?
                ORDER BY a.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.qid, a.chosen, a.correct, a.created_at, q.question, q.options, q.answer,
                       q.explanation, q.source, q.category, q.difficulty
                FROM quiz_attempts a JOIN quiz q ON q.qid = a.qid
                WHERE a.correct = 0
                ORDER BY a.created_at DESC
                """
            ).fetchall()
    out = []
    for r in rows:
        options = json.loads(r["options"]) if r["options"] else []
        out.append(
            {
                "qid": r["qid"],
                "question": r["question"],
                "options": options,
                "chosen": r["chosen"],
                "correct_index": r["answer"],
                "explanation": r["explanation"],
                "source": r["source"],
                "category": r["category"],
                "difficulty": r["difficulty"],
                "answered_at": r["created_at"],
            }
        )
    return out


def quiz_summary(db: Database, user_id: Optional[str] = None) -> dict:
    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) FROM quiz").fetchone()[0]
        if user_id:
            attempted = conn.execute("SELECT COUNT(DISTINCT qid) FROM quiz_attempts WHERE user_id=?", (user_id,)).fetchone()[0]
            correct = conn.execute("SELECT COUNT(*) FROM quiz_attempts WHERE correct=1 AND user_id=?", (user_id,)).fetchone()[0]
            wrong = conn.execute("SELECT COUNT(*) FROM quiz_attempts WHERE correct=0 AND user_id=?", (user_id,)).fetchone()[0]
        else:
            attempted = conn.execute("SELECT COUNT(DISTINCT qid) FROM quiz_attempts").fetchone()[0]
            correct = conn.execute("SELECT COUNT(*) FROM quiz_attempts WHERE correct=1").fetchone()[0]
            wrong = conn.execute("SELECT COUNT(*) FROM quiz_attempts WHERE correct=0").fetchone()[0]
    return {"total": total, "attempted": attempted, "correct": correct, "wrong": wrong}
