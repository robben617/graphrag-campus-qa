"""幂等导入器：原始文件(手册/通知) + 图谱/FAQ/题库 JSON → SQLite。

幂等策略（对齐设计文档 §5.2「导入脚本幂等」、验收 AT-01）：
- 所有实体/文档/条款/关系/FAQ/题库均使用稳定 ID（自 source_path + 内容 hash 或显式 id），
  以 INSERT OR REPLACE 覆盖；重复执行不产生重复行。
- 文档级 hash 用于判定"内容是否变化"（变化则重建其条款）。
- 关系/FAQ 端点做引用完整性校验：引用悬空则报 ImportError 并列出明细，不静默入库。

数据约定（data 目录）：
- data/raw/manifest.json     原始文件元数据清单（标题/分类/发文/日期/状态/版本/适用对象/类型）
- data/kg/entities.json      实体节点（kind: entity|department|process）
- data/kg/relations.json     关系（src, rel, dst, note）
- data/faq.json              常见问答（Question/Answer）
- data/quiz.json             自测题库（第二阶段启用，这里一并入库做规模校验）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..db import Database
from ..errors import ImportError_, StorageUnavailable
from . import parser as doc_parser

PDF_PLACEHOLDER = "【未提取到文本内容】"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ImportResult:
    documents: int = 0
    clauses: int = 0
    entities: int = 0
    relations: int = 0
    faq: int = 0
    quiz: int = 0
    vectors: int = 0
    warnings: List[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "documents": self.documents,
            "clauses": self.clauses,
            "entities": self.entities,
            "relations": self.relations,
            "faq": self.faq,
            "quiz": self.quiz,
            "vectors": self.vectors,
            "warnings": self.warnings,
        }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImportError_(f"缺少数据文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImportError_(f"数据文件 JSON 格式错误: {path}", detail=str(exc)) from exc


def load_manifest(raw_dir: Path) -> List[Dict[str, Any]]:
    mf = _load_json(raw_dir / "manifest.json")
    if not isinstance(mf, list):
        raise ImportError_("manifest.json 应为文件对象数组（列表）。")
    return mf


def _extract_docx_text(file_path: Path) -> str:
    """提取 .docx 文本（段落 + 表格单元格）。"""
    try:
        import docx  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise ImportError_("缺少 python-docx，请执行 pip install python-docx。") from exc
    try:
        document = docx.Document(str(file_path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                parts.append(" ".join(cells))
        return "\n".join(parts)
    except Exception as exc:
        raise ImportError_(f"docx 解析失败: {file_path.name}", detail=str(exc)) from exc


def _kill_word_processes() -> None:
    """强制结束所有残留 WINWORD 进程，避免 Word COM 调用因残留实例卡死。"""
    import subprocess

    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "WINWORD.exe"],
            capture_output=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 - 清理失败不影响后续
        pass


def _extract_doc_text_via_word(file_path: Path) -> str:
    """提取老式 .doc 文本：优先用 Word COM，失败则尝试 LibreOffice。

    健壮性处理（对齐运维约定）：
    - 调用前强制结束残留 WINWORD 进程；
    - DisplayAlerts=0 抑制"确定/重试"等弹窗；
    - Visible=False 后台运行；ReadOnly 打开；
    - 结束后关闭文档并退出 Word，退出后再次清理残留进程。
    """
    import subprocess

    # 调用前清理残留 Word，防止 COM 连接卡死
    _kill_word_processes()

    # 方式一：Word COM（Windows + 已安装 Office）
    try:
        import win32com.client  # noqa: WPS433

        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        try:
            # 抑制所有对话框/提示（确定、重试、忽略等）
            try:
                word.DisplayAlerts = 0
            except Exception:  # noqa: BLE001
                pass
            doc = word.Documents.Open(str(file_path), ReadOnly=True, AddToRecentFiles=False)
            try:
                text = doc.Content.Text
            finally:
                doc.Close(SaveChanges=False)
            return text
        finally:
            try:
                word.Quit()
            except Exception:  # noqa: BLE001
                pass
            _kill_word_processes()
    except Exception:  # noqa: BLE001 - COM 不可用时回退到命令行
        _kill_word_processes()

    # 方式二：libreoffice/soffice 转换
    import shutil

    for exe in ("soffice", "libreoffice"):
        if shutil.which(exe):
            out_dir = file_path.parent / ".tmp_doc_convert"
            out_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [exe, "--headless", "--convert-to", "txt:Text", "--outdir", str(out_dir), str(file_path)],
                check=False, capture_output=True, timeout=120,
            )
            converted = out_dir / (file_path.stem + ".txt")
            if converted.exists():
                text = converted.read_text(encoding="utf-8", errors="ignore")
                converted.unlink(missing_ok=True)
                return text
    raise ImportError_(f"无法解析 .doc 文件（需要 Office/Word COM 或 LibreOffice）: {file_path.name}")


def extract_raw_text(file_path: Path) -> str:
    """从原始文件提取文本。支持 .txt / .pdf / .docx / .doc。

    PDF 用 pypdf 抽取（扫描件无文本层时返回占位提示）；.docx 用 python-docx；
    .doc 优先 Word COM，回退 LibreOffice。
    """
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return doc_parser.read_text_file(file_path)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ImportError_("缺少 PDF 解析库 pypdf，请执行 pip install pypdf。") from exc
        try:
            reader = PdfReader(str(file_path))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = "\n".join(pages)
        except Exception as exc:
            raise ImportError_(f"PDF 解析失败: {file_path.name}", detail=str(exc)) from exc
        if not text.strip():
            return PDF_PLACEHOLDER
        return text
    if suffix == ".docx":
        text = _extract_docx_text(file_path)
        return text if text.strip() else PDF_PLACEHOLDER
    if suffix == ".doc":
        text = _extract_doc_text_via_word(file_path)
        return text if text.strip() else PDF_PLACEHOLDER
    raise ImportError_(f"不支持的文件类型: {file_path.suffix}（支持 .txt / .pdf / .docx / .doc）")


def _doc_identity(meta: Dict[str, Any], file_path: Path, content_hash: str) -> str:
    """稳定文档 ID：优先 manifest 显式 id，否则由相对路径生成。"""
    explicit = meta.get("id")
    if explicit:
        return str(explicit)
    rel = file_path.name
    return f"doc:{_sha256(rel)[:12]}"


def import_raw_documents(db: Database, raw_dir: Path) -> ImportResult:
    """导入原始文件（手册/通知）。manifest 决定元数据与切分模式。"""
    result = ImportResult()
    manifest = load_manifest(raw_dir)
    if not manifest:
        raise ImportError_("manifest.json 为空，未声明任何原始文件。")

    # 预读元数据 → 校验文件存在
    entries: List[Tuple[Dict[str, Any], Path]] = []
    for meta in manifest:
        rel = meta.get("file")
        if not rel:
            raise ImportError_("manifest 条目缺少 file 字段。")
        fp = (raw_dir / rel).resolve()
        if not fp.exists():
            raise ImportError_(f"原始文件不存在: {rel}")
        entries.append((meta, fp))

    with db.session() as conn:
        for meta, fp in entries:
            raw_text = extract_raw_text(fp)
            content_hash = _sha256(raw_text)
            doc_id = _doc_identity(meta, fp, content_hash)
            mode = meta.get("split_mode", "auto")
            blocks = doc_parser.split_into_blocks(raw_text, mode)

            if raw_text.strip() == PDF_PLACEHOLDER:
                result.warnings.append(f"{fp.name}: 未提取到文本（可能是扫描版 PDF，OCR 属后续阶段）")

            conn.execute(
                """
                INSERT OR REPLACE INTO documents
                (doc_id, title, category, issuer, publish_date, effective_date,
                 status, version, applies_to, doc_type, source_path, content_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    doc_id,
                    meta.get("title", fp.name),
                    meta.get("category", ""),
                    meta.get("issuer", ""),
                    meta.get("publish_date", ""),
                    meta.get("effective_date", ""),
                    meta.get("status", "现行有效"),
                    meta.get("version", ""),
                    meta.get("applies_to", ""),
                    meta.get("doc_type", ""),
                    str(fp),
                    content_hash,
                ),
            )
            result.documents += 1

            # 幂等：先删除该文档旧条款，再写入新条款
            conn.execute("DELETE FROM clauses WHERE doc_id = ?", (doc_id,))
            for block in blocks:
                if not block.content:
                    continue
                conn.execute(
                    "INSERT INTO clauses (doc_id, chapter, article_no, content, seq) VALUES (?,?,?,?,?)",
                    (doc_id, block.chapter, block.article_no, block.content, block.seq),
                )
                result.clauses += 1

            # 每条通知/手册若来自章节结构，附加"文档→章节"关系太碎，统一用第 N 条索引
            if mode == "chapter" and any(b.article_no for b in blocks):
                result.warnings.append(f"{fp.name}: 已按章/条语义切分（{len(blocks)} 块）")
    return result


def import_kg(db: Database, kg_dir: Path) -> ImportResult:
    """导入实体 + 关系（含引用完整性校验）。"""
    result = ImportResult()
    ents = _load_json(kg_dir / "entities.json")
    rels = _load_json(kg_dir / "relations.json")
    if not isinstance(ents, list) or not isinstance(rels, list):
        raise ImportError_("entities.json / relations.json 应为数组。")

    known: Dict[str, str] = {}  # eid -> category
    with db.session() as conn:
        for e in ents:
            eid = str(e.get("id", "")).strip()
            if not eid:
                raise ImportError_("实体缺少 id 字段。")
            kind = e.get("kind", "entity")
            name = e.get("name", eid)
            category = e.get("category", "")
            description = e.get("description", "")
            known[eid] = category
            conn.execute(
                "INSERT OR REPLACE INTO entities (eid, kind, name, category, description) VALUES (?,?,?,?,?)",
                (eid, kind, name, category, description),
            )
            result.entities += 1

        # 文档 ID 也纳入可引用端点（例如 ISSUED_BY 关系可指向 dept，或 doc 之间 SUPERSEDES）
        doc_rows = conn.execute("SELECT doc_id, category FROM documents").fetchall()
        for row in doc_rows:
            known[row["doc_id"]] = row["category"]

        dangling: List[str] = []
        for r in rels:
            src = str(r.get("src", ""))
            rel = str(r.get("rel", ""))
            dst = str(r.get("dst", ""))
            note = r.get("note", "")
            if not src or not rel or not dst:
                raise ImportError_("关系缺少 src/rel/dst 字段。")
            if src not in known:
                dangling.append(f"src 未定义: {src}")
            if dst not in known:
                dangling.append(f"dst 未定义: {dst}")
            if dangling:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO relations (src, rel, dst, note) VALUES (?,?,?,?)",
                (src, rel, dst, note),
            )
            result.relations += 1

        if dangling:
            raise ImportError_(
                f"存在 {len(dangling)} 条悬空关系，请修正 entities.json/relations.json：",
                detail="\n".join(dangling),
            )
    return result


def import_faq(db: Database, data_dir: Path) -> int:
    faq = _load_json(Path(data_dir) / "faq.json")
    if not isinstance(faq, list):
        raise ImportError_("faq.json 应为数组。")
    with db.session() as conn:
        for i, item in enumerate(faq):
            qid = str(item.get("id") or f"faq:{i+1}")
            conn.execute(
                "INSERT OR REPLACE INTO faq (qid, question, answer, category, source) VALUES (?,?,?,?,?)",
                (
                    qid,
                    item.get("question", ""),
                    item.get("answer", ""),
                    item.get("category", ""),
                    item.get("source", ""),
                ),
            )
    return len(faq)


def import_quiz(db: Database, data_dir: Path) -> int:
    quiz_path = Path(data_dir) / "quiz.json"
    if not quiz_path.exists():
        return 0
    quiz = _load_json(quiz_path)
    if not isinstance(quiz, list):
        raise ImportError_("quiz.json 应为数组。")
    with db.session() as conn:
        for i, item in enumerate(quiz):
            qid = str(item.get("id") or f"quiz:{i+1}")
            conn.execute(
                "INSERT OR REPLACE INTO quiz (qid, category, difficulty, question, options, answer, explanation, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    qid,
                    item.get("category", ""),
                    item.get("difficulty", "中等"),
                    item.get("question", ""),
                    json.dumps(item.get("options", []), ensure_ascii=False),
                    int(item.get("answer", 0)),
                    item.get("explanation", ""),
                    item.get("source", ""),
                ),
            )
    return len(quiz)


def import_categories(db: Database, kg_dir: Path) -> int:
    """从 entities 中抽取分类，或读显式 categories.json（可选）。"""
    cats_path = kg_dir / "categories.json"
    categories: List[Dict[str, Any]] = []
    if cats_path.exists():
        data = _load_json(cats_path)
        categories = data if isinstance(data, list) else []
    if not categories:
        ents = _load_json(kg_dir / "entities.json")
        seen = {}
        for e in ents:
            c = e.get("category", "").strip()
            if c and c not in seen:
                seen[c] = e.get("description", "")
        categories = [{"name": k, "description": v} for k, v in seen.items()]
    with db.session() as conn:
        for c in categories:
            conn.execute(
                "INSERT OR REPLACE INTO categories (name, description) VALUES (?,?)",
                (str(c.get("name", "")).strip(), str(c.get("description", ""))),
            )
    return len(categories)


def run_import(db: Database, data_dir: Path, build_vectors: bool = True) -> ImportResult:
    """执行完整导入：categories + raw + kg + faq + quiz（+ 向量索引），记录日志。"""
    db.init_schema()
    raw_dir = Path(data_dir) / "raw"
    kg_dir = Path(data_dir) / "kg"

    counts = {
        "categories": 0, "documents": 0, "clauses": 0, "entities": 0,
        "relations": 0, "faq": 0, "quiz": 0, "vectors": 0,
    }
    warnings: List[str] = []
    started = _now()

    counts["categories"] = import_categories(db, kg_dir)
    r_raw = import_raw_documents(db, raw_dir)
    r_kg = import_kg(db, kg_dir)
    faq_n = import_faq(db, data_dir)
    quiz_n = import_quiz(db, data_dir)

    counts["documents"] = r_raw.documents
    counts["clauses"] = r_raw.clauses
    counts["entities"] = r_kg.entities
    counts["relations"] = r_kg.relations
    counts["faq"] = faq_n
    counts["quiz"] = quiz_n
    warnings.extend(r_raw.warnings)

    # 向量索引（幂等：只补无向量的条款）
    if build_vectors:
        try:
            from ..embeddings import get_embedder
            from ..config import get_settings
            from ..retrieval.vector import ensure_vector_index

            counts["vectors"] = ensure_vector_index(db, get_embedder(get_settings()))
        except Exception as exc:  # noqa: BLE001 - 向量构建失败不阻塞导入
            warnings.append(f"向量索引构建失败（已跳过，可用关键词检索）: {exc}")

    with db.session() as conn:
        conn.execute(
            "INSERT INTO import_log (started_at, finished_at, counts_json, ok) VALUES (?,?,?,?)",
            (_now(), _now(), json.dumps(counts, ensure_ascii=False), 1),
        )

    result = ImportResult(**{k: v for k, v in counts.items() if k != "categories"})
    result.warnings = warnings
    return result
