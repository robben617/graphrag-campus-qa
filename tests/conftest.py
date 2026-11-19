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
    """保持默认测试离线，模型相关场景由各测试显式配置。"""
    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "EMBEDDING_API_KEY", "DIFY_RETRIEVAL_API_KEY"):
        os.environ[name] = ""
    os.environ["LLM_ENABLED"] = "false"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """清理被环境拦截时给出显式提示，避免静默残留。"""
    if _PURGE_BLOCKED:
        terminalreporter.write_sep(
            "-",
            "警告：部分测试临时目录未能清理（被本机的安全删除钩子拦截）。"
            f"请手工删除 {_work_root()} 下的测试临时目录。",
        )


def _fresh_dir(prefix: str) -> Path:
    """在可写工作根目录下建一个全新子目录（沙箱兼容）。"""
    p = _work_root() / f"{prefix}-{time.time_ns()}"
    p.mkdir(parents=True, exist_ok=False)
    return p


@pytest.fixture()
def tmp_data_dir() -> Path:
    """供导入测试修改的独立语料目录。"""
    dest = _fresh_dir("testdata")
    try:
        for directory in ("raw", "kg"):
            shutil.copytree(DATA_DIR / directory, dest / directory)
        for filename in ("faq.json", "quiz.json", "evaluation.json"):
            shutil.copy2(DATA_DIR / filename, dest / filename)
        yield dest
    finally:
        _purge(dest)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "school_kb.db"))
    database.init_schema()
    return database


@pytest.fixture(scope="session")
def imported_snapshot():
    """从语料构建一次基础库。"""
    from app.ingest.importer import run_import

    dest = _fresh_dir("testseed")
    try:
        database = Database(str(dest / "school_kb.db"))
        run_import(database, DATA_DIR)
        yield database.path
    finally:
        _purge(dest)


@pytest.fixture()
def imported_db(imported_snapshot) -> Database:
    """每个用例使用各自独立的数据库快照。"""
    from app.backup import backup_db

    dest = _fresh_dir("testdb")
    try:
        database = Database(str(dest / "school_kb.db"))
        backup_db(imported_snapshot, database.path)
        yield database
    finally:
        _purge(dest)


@pytest.fixture()
def client(imported_db):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.web.server import create_app

    with TestClient(create_app(Settings(db_path=imported_db.path))) as test_client:
        yield test_client
