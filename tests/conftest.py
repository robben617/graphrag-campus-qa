"""pytest 公共夹具。

注意：本环境（沙箱）下 pytest 内置 tmp_path 与 tempfile.mkdtemp 创建的目录会被
拒绝写入/删除，故统一使用 pathlib 在工作区内自建临时目录。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.db import Database

DATA_DIR = PROJECT_ROOT / "data"
_WORK_TMP = PROJECT_ROOT / ".tmp"


def pytest_configure(config):
    """测试统一禁用本地大模型（改用哈希向量器），避免每个 fixture 加载大模型过慢。

    本地模型推理在 test_phase3 中通过 ml_local 直接、显式地验证。
    """
    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"


def _fresh_dir(prefix: str) -> Path:
    """用 pathlib 自建可写临时目录（沙箱兼容）。"""
    import time

    p = _WORK_TMP / f"{prefix}-{time.time_ns()}"
    p.mkdir(parents=True, exist_ok=False)
    return p


@pytest.fixture()
def tmp_data_dir() -> Path:
    """把 data 目录拷贝到工作区临时目录（含 PDF），供导入测试使用。"""
    dest = _fresh_dir("testdata")
    shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)
    yield dest
    shutil.rmtree(dest, ignore_errors=True)


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
    import os

    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"

    from app.ingest.importer import run_import

    database = Database(str(tmp_data_dir / "school_kb.db"))
    run_import(database, tmp_data_dir)
    return database
