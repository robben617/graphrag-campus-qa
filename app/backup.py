"""备份/恢复脚本（第三阶段，§9 容器化部署、备份恢复）。

用法：
  python -m app.backup backup [--out backups/school_kb_<ts>.db]
  python -m app.backup restore <backup.db>

备份策略：SQLite 在线备份（backup API，安全一致性快照），不阻塞读。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import get_settings

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def backup_db(src: str, dst: str) -> str:
    """用 SQLite backup API 做一致性备份。返回备份文件路径。"""
    src_conn = sqlite3.connect(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return str(dst_path)


def restore_db(backup: str, dst: str) -> str:
    """从备份恢复：校验备份有效后覆盖目标库。"""
    if not Path(backup).exists():
        raise FileNotFoundError(f"备份文件不存在: {backup}")
    # 校验备份可打开
    conn = sqlite3.connect(backup)
    conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
    conn.close()
    shutil.copy2(backup, dst)
    return dst


def cmd_backup(args) -> int:
    settings = get_settings()
    src = settings.db_path
    out = args.out or f"backups/school_kb_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    path = backup_db(src, out)
    print(f"备份完成：{path}")
    return 0


def cmd_restore(args) -> int:
    settings = get_settings()
    path = restore_db(args.backup, settings.db_path)
    print(f"恢复完成：{path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.backup", description="知识库备份/恢复")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="备份数据库")
    b.add_argument("--out", default=None, help="备份输出路径")
    r = sub.add_parser("restore", help="从备份恢复")
    r.add_argument("backup", help="备份文件路径")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "backup":
        return cmd_backup(args)
    return cmd_restore(args)


if __name__ == "__main__":
    sys.exit(main())
