"""配置加载：读取 .env（若存在）与环境变量，提供全局 Settings。

约定：键名见 .env.example。环境变量优先于 .env 文件。
兼容性：
- .env 支持 UTF-8 或 GBK 编码（自动探测）。
- 兼容 DeepSeek 风格变量名（DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL_NAME）
  与通用 OpenAI 兼容变量名（LLM_*），二者任一存在即可启用 LLM。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict:
    """读取 .env 为 dict，自动处理 UTF-8/GBK 编码。"""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


_dotenv_path = Path(os.environ.get("SCHOOL_ENV_FILE", PROJECT_ROOT / ".env"))
_dotenv_values = _read_dotenv(_dotenv_path)
for _k, _v in _dotenv_values.items():
    os.environ.setdefault(_k, _v)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _resolve(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


def _llm_api_key() -> str:
    return os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")


def _llm_base_url() -> str:
    return (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com/v1"
    )


def _llm_model() -> str:
    return os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL_NAME") or "deepseek-chat"


def _llm_enabled() -> bool:
    if _env_bool("LLM_ENABLED", False):
        return True
    return bool(_llm_api_key())


def _embedding_key() -> str:
    return os.environ.get("EMBEDDING_API_KEY", "")


def _embedding_base_url() -> str:
    return os.environ.get("EMBEDDING_BASE_URL", "")


@dataclass(frozen=True)
class Settings:
    # 存储
    db_path: str = field(
        default_factory=lambda: _resolve(os.environ.get("SCHOOL_DB_PATH", "data/school_kb.db"))
    )
    data_dir: str = field(
        default_factory=lambda: _resolve(os.environ.get("SCHOOL_DATA_DIR", "data"))
    )
    # LLM（可选，兼容 DeepSeek / OpenAI 风格）
    llm_enabled: bool = field(default_factory=_llm_enabled)
    llm_base_url: str = field(default_factory=_llm_base_url)
    llm_api_key: str = field(default_factory=_llm_api_key)
    llm_model: str = field(default_factory=_llm_model)
    llm_timeout: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT", 60))
    # Embedding（可选；未配置则用本地确定性向量器兜底）
    embedding_enabled: bool = field(default_factory=lambda: bool(_embedding_key()))
    embedding_base_url: str = field(default_factory=_embedding_base_url)
    embedding_api_key: str = field(default_factory=_embedding_key)
    embedding_model: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    )
    embedding_dim: int = field(default_factory=lambda: _env_int("EMBEDDING_DIM", 256))
    # 服务参数
    max_question_len: int = field(default_factory=lambda: _env_int("MAX_QUESTION_LEN", 2000))
    top_k: int = field(default_factory=lambda: _env_int("TOP_K", 5))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    # 会话
    session_history_limit: int = field(default_factory=lambda: _env_int("SESSION_HISTORY_LIMIT", 20))
    # CORS（前端独立部署时的跨域来源，逗号分隔）
    cors_origins: str = field(
        default_factory=lambda: os.environ.get("CORS_ORIGINS", "*")
    )
    # 本地模型（第三阶段）
    local_models_dir: str = field(
        default_factory=lambda: _resolve(os.environ.get("LOCAL_MODELS_DIR", "local_models/models"))
    )
    local_embedding_model: str = field(
        default_factory=lambda: os.environ.get("LOCAL_EMBEDDING_MODEL", "BAAI--bge-small-zh-v1.5")
    )
    local_reranker_model: str = field(
        default_factory=lambda: os.environ.get("LOCAL_RERANKER_MODEL", "BAAI--bge-reranker-base")
    )
    # 本地模型开关：auto（可用则用，否则降级）/ on / off
    use_local_embedding: str = field(
        default_factory=lambda: os.environ.get("USE_LOCAL_EMBEDDING", "auto")
    )
    use_local_reranker: str = field(
        default_factory=lambda: os.environ.get("USE_LOCAL_RERANKER", "auto")
    )
    ml_device: str = field(
        default_factory=lambda: os.environ.get("ML_DEVICE", "cpu")
    )
    # Dify 外部知识库适配（可选；未配置 KEY 则不注册 /dify/retrieval 路由）
    dify_retrieval_api_key: str = field(
        default_factory=lambda: os.environ.get("DIFY_RETRIEVAL_API_KEY", "")
    )
    dify_max_top_k: int = field(default_factory=lambda: _env_int("DIFY_MAX_TOP_K", 20))

    @property
    def data_raw_dir(self) -> str:
        return str(Path(self.data_dir) / "raw")

    @property
    def kg_dir(self) -> str:
        return str(Path(self.data_dir) / "kg")

    @property
    def cors_origin_list(self) -> list:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return origins if origins else ["*"]

    @property
    def llm_configured(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)

    @property
    def local_embedding_path(self) -> str:
        return str(Path(self.local_models_dir) / self.local_embedding_model)

    @property
    def local_reranker_path(self) -> str:
        return str(Path(self.local_models_dir) / self.local_reranker_model)


def get_settings() -> Settings:
    """每次调用读取当前环境，测试中便于覆盖。"""
    return Settings()


def settings_from_env() -> Settings:
    return Settings()
