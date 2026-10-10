"""命令行入口：python -m app.cli <命令>

命令：
  import              幂等导入数据（手册/通知 + 图谱 + FAQ + 题库 + 向量）
  stats               统计与最小规模校验
  ask "问题"          单轮问答（GraphRAG）
  chat                交互式多轮问答（命令行会话）
  serve [--port]      启动 FastAPI（REST API + Swagger）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出以避免中文/符号编码报错
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from . import __version__
from .config import get_settings
from .db import Database
from .errors import AppError


def cmd_import(args) -> int:
    from .ingest.importer import run_import
    from .ingest.stats import check_minimum, collect, format_report

    settings = get_settings()
    db = Database(settings.db_path)
    result = run_import(db, Path(settings.data_dir))
    print("导入完成：")
    for k, v in result.to_dict().items():
        if k == "warnings":
            continue
        print(f"  {k:<12}: {v}")
    if result.warnings:
        print("\n提示：")
        for w in result.warnings:
            print(f"  - {w}")

    stats = collect(db)
    checks, ok = check_minimum(stats)
    print("\n" + format_report(stats, checks, ok))
    return 0 if ok else 1


def cmd_stats(args) -> int:
    from .ingest.stats import check_minimum, collect, format_report

    settings = get_settings()
    db = Database(settings.db_path)
    db.init_schema()
    stats = collect(db)
    checks, ok = check_minimum(stats)
    print(format_report(stats, checks, ok))
    return 0 if ok else 1


def cmd_ask(args) -> int:
    from .service.qa import QAService

    settings = get_settings()
    db = Database(settings.db_path)
    db.init_schema()
    qa = QAService(db, settings)
    try:
        ans = qa.ask(args.question)
    except AppError as exc:
        print(f"[错误] {exc.message}")
        return 1

    print(f"问题：{ans.question}")
    print(f"模式：{ans.mode}{'（降级）' if ans.degraded else ''}")
    print("-" * 40)
    print(ans.answer)
    if ans.conflict_notice:
        print(f"\n⚠ {ans.conflict_notice}")
    if ans.degradation_notice:
        print(f"\n⚠ {ans.degradation_notice}")
    if ans.citations:
        print("\n引用来源：")
        for i, c in enumerate(ans.citations, 1):
            loc = f"第{c.article_no}条" if c.article_no else ""
            if c.chapter and loc:
                loc = f"{c.chapter} {loc}"
            print(f"  [{i}] 《{c.doc_title}》{loc}")
            print(f"      {c.snippet[:80]}")
    if ans.entities:
        names = [e["name"] for e in ans.entities]
        print(f"\n相关实体：{', '.join(names)}")
    print(f"\n{ans.disclaimer}")
    return 0


def cmd_chat(args) -> int:
    """交互式多轮问答（命令行）。"""
    from .service.conversation import ConversationService
    from .service.qa import QAService

    settings = get_settings()
    db = Database(settings.db_path)
    db.init_schema()
    qa = QAService(db, settings)
    conv = ConversationService(db, settings)
    session = conv.create()

    print("交互式问答（输入 exit / quit 退出）")
    history: list = []
    while True:
        try:
            question = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", "退出"}:
            break
        rewritten = conv.rewrite_question(question, history)
        ans = qa.ask(rewritten, history=history)
        conv.add_message(session.session_id, "user", question)
        conv.add_message(session.session_id, "assistant", ans.answer, citations=ans.citations)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": ans.answer})
        if rewritten != question:
            print(f"（理解：{rewritten}）")
        print(f"\n助手[{ans.mode}] > {ans.answer}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web.server import create_app

    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=settings.log_level.lower())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description="校园知识问答系统（GraphRAG）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("import", help="幂等导入数据（含向量索引）")
    sub.add_parser("stats", help="统计与最小规模校验")

    pa = sub.add_parser("ask", help="单轮问答")
    pa.add_argument("question", help="提问内容")
    pa.add_argument("--top-k", type=int, default=None, help="检索条款数（默认取配置）")

    sub.add_parser("chat", help="交互式多轮问答")

    ps = sub.add_parser("serve", help="启动 Web 服务（REST API）")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "import": cmd_import,
        "stats": cmd_stats,
        "ask": cmd_ask,
        "chat": cmd_chat,
        "serve": cmd_serve,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
