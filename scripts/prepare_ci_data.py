"""CI 前置：剔除 manifest 中「未随仓库分发」的条目，用仓库内样例数据跑测试。

背景
----
学生手册 PDF 等原始文件版权归校方，**不入版本库**（见 `.gitignore` 与 README「数据准备」）。
而 `app/ingest/importer.py` 对 manifest 里缺失的文件是**显式报错**（不静默跳过），
因此在缺少这些文件的干净 clone / CI 环境里，导入会直接失败、绝大多数用例会 ERROR。

本脚本**只在 CI 使用**：读 `data/raw/manifest.json`，剔除对应文件不存在的条目后写回，
让测试能在「已分发样例」上运行。

**本地开发不要执行**：本地放好原始文件后 `python -m app.cli import` 即可完整导入
（8 文档 / 827 条款），执行本脚本会破坏清单。

用法：
    python scripts/prepare_ci_data.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"


def main() -> int:
    manifest = RAW / "manifest.json"
    if not manifest.exists():
        print(f"[prepare_ci_data] 未找到 {manifest}，跳过")
        return 0

    entries = json.loads(manifest.read_text(encoding="utf-8"))
    kept, dropped = [], []
    for entry in entries:
        rel = entry.get("file")
        (kept if rel and (RAW / rel).exists() else dropped).append(
            entry if rel and (RAW / rel).exists() else rel
        )

    if not dropped:
        print("[prepare_ci_data] 全部原始文件在位，无需处理（完整语料环境）")
        return 0

    manifest.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[prepare_ci_data] 已剔除 {len(dropped)} 条未分发文件：{dropped}")
    print(f"[prepare_ci_data] 保留 {len(kept)} 条，CI 将以仓库内样例数据运行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
