"""本地模型推理封装：BGE Embedding + BGE Reranker（第三阶段）。

设计：
- 惰性加载：首次调用时才加载模型（避免启动时占用内存）；
- 缓存单例：进程内复用，避免重复加载；
- 优雅降级：模型文件缺失 / torch 不可用 / 未启用时，返回 None，由调用方回退到
  （Embedding→哈希向量器 / Reranker→不重排）。
- 线程安全：加载用锁保护。

模型路径来自 Settings.local_embedding_path / local_reranker_path。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import Settings

_lock = threading.Lock()
_embedder_cache: Optional[object] = None
_embedder_key: Optional[tuple] = None
_reranker_cache: Optional[object] = None
_reranker_key: Optional[tuple] = None
_embedding_unavailable = False
_reranker_unavailable = False


def _model_exists(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    has_safetensors = any(p.rglob("*.safetensors"))
    has_bin = any(p.rglob("pytorch_model.bin"))
    has_onnx = any(p.rglob("model.onnx"))
    return has_safetensors or has_bin or has_onnx


def _resolve_model_dir(path: str) -> str:
    """HuggingFace 快照目录（snapshots/master/）下模型文件才完整，优先指向它。"""
    p = Path(path)
    # 若直接是模型目录（含 config.json 且含权重），直接返回
    if (p / "config.json").exists() and _model_exists(path):
        return str(p)
    snapshots = list(p.rglob("snapshots"))
    for snap in snapshots:
        for child in snap.iterdir():
            if child.is_dir() and _model_exists(str(child)):
                return str(child)
    # 兜底：返回原路径（调用方加载失败会降级）
    return str(p)


def _load_embedder(settings: Settings):
    """加载 BGE Embedding 模型（sentence_transformers）。失败返回 None。"""
    global _embedder_cache, _embedder_key, _embedding_unavailable
    key = (settings.local_embedding_path, settings.ml_device)
    with _lock:
        if _embedder_cache is not None and _embedder_key == key:
            return _embedder_cache
        if _embedding_unavailable and _embedder_key == key:
            return None
        try:
            if not _model_exists(settings.local_embedding_path):
                _embedding_unavailable = True
                _embedder_key = key
                return None
            from sentence_transformers import SentenceTransformer  # noqa: WPS433

            model = SentenceTransformer(_resolve_model_dir(settings.local_embedding_path), device=settings.ml_device)
            _embedder_cache = model
            _embedder_key = key
            return model
        except Exception:  # noqa: BLE001
            _embedding_unavailable = True
            _embedder_key = key
            return None


def _load_reranker(settings: Settings):
    """加载 BGE Reranker（CrossEncoder）。失败返回 None。"""
    global _reranker_cache, _reranker_key, _reranker_unavailable
    key = (settings.local_reranker_path, settings.ml_device)
    with _lock:
        if _reranker_cache is not None and _reranker_key == key:
            return _reranker_cache
        if _reranker_unavailable and _reranker_key == key:
            return None
        try:
            if not _model_exists(settings.local_reranker_path):
                _reranker_unavailable = True
                _reranker_key = key
                return None
            from sentence_transformers import CrossEncoder  # noqa: WPS433

            model = CrossEncoder(_resolve_model_dir(settings.local_reranker_path), device=settings.ml_device)
            _reranker_cache = model
            _reranker_key = key
            return model
        except Exception:  # noqa: BLE001
            _reranker_unavailable = True
            _reranker_key = key
            return None


def local_embedder_available(settings: Settings) -> bool:
    if settings.use_local_embedding == "off":
        return False
    if settings.use_local_embedding == "on":
        return True
    # auto：模型文件存在即视为可用（真正加载在首次调用时验证）
    return _model_exists(settings.local_embedding_path)


def local_reranker_available(settings: Settings) -> bool:
    if settings.use_local_reranker == "off":
        return False
    if settings.use_local_reranker == "on":
        return True
    return _model_exists(settings.local_reranker_path)


def encode_local(settings: Settings, texts: Sequence[str]) -> Optional[List[List[float]]]:
    """本地 BGE Embedding；不可用时返回 None。"""
    model = _load_embedder(settings)
    if model is None:
        return None
    try:
        vecs = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]
    except Exception:  # noqa: BLE001
        return None


def rerank_local(
    settings: Settings, query: str, passages: Sequence[str], top_k: int
) -> Optional[List[Tuple[int, float]]]:
    """本地 BGE Reranker：返回 [(原文下标, 得分)] 降序，取 top_k。不可用返回 None。"""
    model = _load_reranker(settings)
    if model is None or not passages:
        return None
    try:
        pairs = [(query, p) for p in passages]
        scores = model.predict(pairs, show_progress_bar=False)
        ranked = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
        return [(i, float(scores[i])) for i in ranked[:top_k]]
    except Exception:  # noqa: BLE001
        return None
