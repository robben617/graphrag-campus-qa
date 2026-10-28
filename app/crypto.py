"""用户密钥的加解密（BYOK 方案的存储安全层）。

为什么不能明文存：
用户把自己购买的大模型 API Key 交给本系统代为调用，一旦明文落库，任何拿到
`data/school_kb.db` 的人（备份、误提交、容器镜像、同事拷贝）都能直接盗刷。
因此密钥必须加密存储，且**永不返回给前端**。

主密钥从哪来（按优先级）：
1. 环境变量 `LLM_CONFIG_SECRET` —— 生产部署必须用这条，随密钥管理下发；
2. 自动生成 `data/.llm_secret`（首次运行）—— 仅用于本地开发和演示，
   省掉"跑不起来"的门槛，但日志会明确警告。

注意：切换主密钥会导致已存密文无法解密。这是有意的取舍——宁可让用户重填一次，
也不要为了兼容旧密文而削弱密钥强度。解密失败时按"未配置"处理，不让系统崩。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("school_agent.crypto")

_SECRET_FILE_NAME = ".llm_secret"


class SecretBox:
    """Fernet 加解密封装。

    Fernet = AES-128-CBC + HMAC-SHA256，带完整性校验：密文被篡改会直接抛
    InvalidToken，而不是解出一段乱码再喂给下游。
    """

    def __init__(self, master_secret: str, source: str = "env"):
        if not master_secret:
            raise ValueError("master_secret 不能为空")
        # Fernet 要求 32 字节 urlsafe-base64；用 SHA-256 把任意长度口令压成定长密钥
        digest = hashlib.sha256(master_secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        self.source = source

    def encrypt(self, plain: str) -> str:
        if plain is None or plain == "":
            return ""
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密失败返回空串：主密钥换过时视为未配置，而不是抛异常打断请求。"""
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            logger.warning("llm_key_decrypt_failed reason=%s", type(exc).__name__)
            return ""


def key_hint(plain: str) -> str:
    """只保留首尾少量字符，够用户认出是哪把 key，不足以还原。"""
    if not plain:
        return ""
    if len(plain) <= 8:
        return "*" * len(plain)
    return f"{plain[:3]}…{plain[-4:]}"


def _load_or_create_file_secret(secret_path: Path) -> str:
    if secret_path.exists():
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError as exc:  # pragma: no cover - 权限异常等
            logger.warning("llm_secret_read_failed path=%s err=%s", secret_path, exc)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    secret_path.write_text(value, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限
        pass
    logger.warning(
        "llm_secret_generated path=%s —— 仅限本地使用；生产环境请设置 LLM_CONFIG_SECRET",
        secret_path,
    )
    return value


def build_secret_box(explicit_secret: str = "", secret_dir: Optional[str] = None) -> SecretBox:
    """按「环境变量 → 本地密钥文件」顺序构造密钥箱。"""
    env_secret = explicit_secret or os.environ.get("LLM_CONFIG_SECRET", "")
    if env_secret:
        return SecretBox(env_secret, source="env")
    base_dir = Path(secret_dir) if secret_dir else Path(".")
    path = base_dir / _SECRET_FILE_NAME
    return SecretBox(_load_or_create_file_secret(path), source="file")
