"""SQLite 存储层：连接管理、Schema 初始化、基础查询。

数据模型对应设计文档 §5.1（SQLite 起步，第二阶段迁移 Neo4j）：
- categories  主题分类（≥5）
- entities    实体节点：kind ∈ {entity 领域对象, department 部门, process 流程}
- documents   源文件（手册/通知），含元数据（发布时间/生效/状态/版本/适用对象）
- clauses     条款/段落块：chapter 章、article_no 条号、content 原文
- relations   关系（src, rel, dst, note），端点引用 entities/documents 的稳定 ID
- faq         FAQ 问答对（Question/Answer）
- quiz        自测题库（第二阶段启用）
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .errors import StorageUnavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entities (
    eid         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- entity | department | process
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '',
    issuer        TEXT NOT NULL DEFAULT '',
    publish_date  TEXT NOT NULL DEFAULT '',
    effective_date TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '现行有效',
    version       TEXT NOT NULL DEFAULT '',
    applies_to    TEXT NOT NULL DEFAULT '',
    doc_type      TEXT NOT NULL DEFAULT '', -- 手册 | 通知
    source_path   TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clauses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chapter     TEXT NOT NULL DEFAULT '',
    article_no  TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clauses_doc ON clauses(doc_id);

CREATE TABLE IF NOT EXISTS relations (
    src     TEXT NOT NULL,
    rel     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (src, rel, dst)
);

CREATE TABLE IF NOT EXISTS faq (
    qid      TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    source   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quiz (
    qid         TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    difficulty  TEXT NOT NULL DEFAULT '中等',
    question    TEXT NOT NULL,
    options     TEXT NOT NULL,   -- JSON 数组
    answer      INTEGER NOT NULL, -- 正确选项下标（0 起）
    explanation TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS import_log (
    run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT,
    counts_json TEXT NOT NULL DEFAULT '{}',
    ok        INTEGER NOT NULL DEFAULT 1
);

-- ============ 第二阶段：会话 / 向量 / 掌握 / 答题 ============

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'student',   -- student | teacher | admin
    api_token   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'anonymous',
    title       TEXT NOT NULL DEFAULT '新会话',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,             -- user | assistant
    content     TEXT NOT NULL,
    citations   TEXT NOT NULL DEFAULT '[]', -- JSON 数组
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS vector_index (
    clause_id   INTEGER PRIMARY KEY REFERENCES clauses(id) ON DELETE CASCADE,
    doc_id      TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      TEXT NOT NULL,              -- JSON 数组（float 列表）
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_vector_doc ON vector_index(doc_id);

CREATE TABLE IF NOT EXISTS mastery (
    item_id     TEXT NOT NULL,             -- 实体 eid 或条款 clause:{id}
    user_id     TEXT NOT NULL DEFAULT 'anonymous',
    item_type   TEXT NOT NULL,             -- entity | clause
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,             -- mastered | unmastered
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_mastery_user ON mastery(user_id);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qid         TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'anonymous',
    chosen      INTEGER,                   -- 所选选项下标（0 起）；未作答为 NULL
    correct     INTEGER,                   -- 1 正确 / 0 错误
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_qid ON quiz_attempts(qid);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user ON quiz_attempts(user_id);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    recall_at_k REAL,
    factuality  REAL,
    citation_cov REAL,
    total       INTEGER,
    report_json TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    """封装 sqlite3 连接：自动建目录、统一异常、线程安全写。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        try:
            p = Path(self.path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        except sqlite3.Error as exc:
            raise StorageUnavailable(detail=f"打开数据库失败: {exc}") from exc

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self.connect()
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise StorageUnavailable(detail=f"数据库操作失败: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def init_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(SCHEMA)

    def is_empty(self) -> bool:
        try:
            with self.session() as conn:
                cur = conn.execute("SELECT COUNT(*) AS c FROM documents")
                docs = cur.fetchone()["c"]
                cur = conn.execute("SELECT COUNT(*) AS c FROM entities")
                ents = cur.fetchone()["c"]
                return docs == 0 and ents == 0
        except StorageUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - 防御
            raise StorageUnavailable(detail=str(exc)) from exc
