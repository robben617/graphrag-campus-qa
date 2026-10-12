"""pytest 公共夹具。

关于测试工作目录的选取：

1. 优先使用系统临时目录（tempfile.gettempdir()）下的独立子目录。这是 pytest 的
   常规做法，不污染项目工作区。

2. 本机（WorkBuddy 运行环境）对 os.remove / os.unlink / os.rmdir / shutil.rmtree
   装了「安全删除钩子」：当一次删除涉及的文件数超过阈值时，会要求确认；测试进程
   无法应答，钩子便以 SystemExit(1) 中止——它发生在 fixture teardown，会把本已
   通过的用例统统记为 ERROR。该钩子对 **OS 临时目录内的路径直接放行**，因此把
   工作目录放在临时目录下即可完全规避。

3. 若临时目录不可写（部分沙箱会拒绝），自动退回项目内的 .tmp/。此时若 teardown
   仍被上述钩子拦截，`_purge()` 会吞掉异常并在测试结束时给出醒目警告——清理失败
   绝不能影响测试结论。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.db import Database

DATA_DIR = PROJECT_ROOT / "data"
_LEGACY_TMP = PROJECT_ROOT / ".tmp"

# 进程内缓存的工作根目录；None 表示尚未探测
_WORK_ROOT: "Path | None" = None
# teardown 清理是否被环境拦截过（用于结束时给出提示）
_PURGE_BLOCKED = False


def _work_root() -> Path:
    """挑一个可写的测试工作根目录（结果进程内缓存）。"""
    global _WORK_ROOT
    if _WORK_ROOT is not None:
        return _WORK_ROOT

    candidates = [
        Path(tempfile.gettempdir()) / "school-agent-tests",
        _LEGACY_TMP,
    ]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f".probe-{os.getpid()}"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except Exception:  # noqa: BLE001 - 不可写就换下一个候选
            continue
        _WORK_ROOT = cand
        return cand

    _WORK_ROOT = _LEGACY_TMP
    return _WORK_ROOT


def _purge(path: Path) -> None:
    """尽力删除测试临时目录；任何异常都不得影响测试结果。

    注意 `shutil.rmtree(..., ignore_errors=True)` 只吞 OSError，
    吞不掉安全删除钩子抛出的 SystemExit，故此处显式兜 BaseException。
    """
    global _PURGE_BLOCKED
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException:  # noqa: BLE001 - 环境限制，放弃清理
        _PURGE_BLOCKED = True


def pytest_configure(config):
    """测试统一禁用本地大模型（改用哈希向量器），避免每个 fixture 加载大模型过慢。

    本地模型推理在 test_phase3 中通过 ml_local 直接、显式地验证。
    """
    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """清理被环境拦截时给出显式提示，避免静默残留。"""
    if _PURGE_BLOCKED:
        terminalreporter.write_sep(
            "-",
            "警告：部分测试临时目录未能清理（被本机的安全删除钩子拦截）。"
            f"请手工删除 {_work_root()} 下的 testdata-* 目录。",
        )


def _reset_users(db_path: Path) -> None:
    """清空从真实库拷贝来的用户记录，让测试从「系统未初始化」的确定状态开始。

    为什么必须做：`data/school_kb.db` 是开发/演示用过的真实库，里面可能残留账号。
    而「创建用户」的鉴权规则是「库中**一个用户都没有**时，首个账号免鉴权（bootstrap）；
    此后创建需 admin」—— 残留用户会让所有建号操作都变成需要 admin，与测试意图冲突。

    用户 / 会话 / 掌握 / 答题记录都属于运行时状态，与"数据导入"无关，
    因此在这里重置，而不是在 `run_import` 里清（导入不该动用户表）。
    """
    if not db_path.exists():
        return
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM users")
        conn.commit()
    except sqlite3.Error:  # pragma: no cover - 夹具不应因重置失败而中断
        pass
    finally:
        if conn is not None:
            conn.close()


def _fresh_dir(prefix: str) -> Path:
    """在可写工作根目录下建一个全新子目录（沙箱兼容）。"""
    p = _work_root() / f"{prefix}-{time.time_ns()}"
    p.mkdir(parents=True, exist_ok=False)
    return p


@pytest.fixture()
def tmp_data_dir() -> Path:
    """把 data 目录拷贝到测试临时目录（含 PDF），供导入测试使用。

    拷贝后立即清空 users —— 测试需要从「系统未初始化」的确定状态开始，
    而真实库里可能残留演示账号（原因见 _reset_users）。
    """
    dest = _fresh_dir("testdata")
    shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)
    _reset_users(dest / "school_kb.db")
    yield dest
    _purge(dest)


@pytest.fixture()
def db(tmp_data_dir: Path) -> Database:
    database = Database(str(tmp_data_dir / "school_kb.db"))
    database.init_schema()
    return database


@pytest.fixture()
def imported_db(tmp_data_dir: Path) -> Database:
    """执行一次完整导入后的数据库。

    测试环境禁用本地 BGE 模型（改用哈希向量器），避免每个 fixture 都加载大模型、
    构建 512 维向量导致测试过慢。本地模型推理由 test_phase3 中单独、显式地验证。
    """
    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"

    from app.ingest.importer import run_import

    database = Database(str(tmp_data_dir / "school_kb.db"))
    run_import(database, tmp_data_dir)
    return database
