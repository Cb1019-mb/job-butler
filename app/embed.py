"""Embedding 客户端:调本地 vLLM embedding 服务(localhost:8001/v1/embeddings)。

模型:BAAI/bge-small-zh-v1.5(512 维)。查询侧加 BGE 检索指令前缀,文档侧不加。
模型配置改 settings 后下一次请求生效。

W3 改进:连接失败时**短路**(60s 内直接抛),避免 30s 超时阻塞业务流。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Sequence

import httpx
import numpy as np

from .modules import settings as _settings

log = logging.getLogger(__name__)

BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章: "

# 短路保护:连接失败后 60s 内直接失败,不实际连接
_short_circuit_lock = threading.Lock()
_short_circuit_until: float = 0.0
_short_circuit_msg: str = ""


class EmbeddingUnavailable(RuntimeError):
    pass


def _is_short_circuited() -> bool:
    with _short_circuit_lock:
        return time.monotonic() < _short_circuit_until


def _mark_short_circuit(msg: str) -> None:
    global _short_circuit_until, _short_circuit_msg
    with _short_circuit_lock:
        _short_circuit_until = time.monotonic() + 60.0
        _short_circuit_msg = msg
    log.warning("embedding 短路 60s: %s", msg)


def _post_embeddings(inputs: list[str]) -> np.ndarray:
    if _is_short_circuited():
        raise EmbeddingUnavailable(f"embedding 后端短路: {_short_circuit_msg}")
    base_url = _settings.get("embed.base_url")
    model = _settings.get("embed.model")
    api_key = _settings.get("embed.api_key")
    try:
        resp = httpx.post(
            f"{base_url}/embeddings",
            json={"model": model, "input": inputs},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=1.5, read=20.0, write=5.0, pool=1.5),
            # 不走系统代理,防止本机代理劫持 localhost 端点(返回 404)
            trust_env=False,
        )
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError) as e:
        _mark_short_circuit(str(e))
        raise EmbeddingUnavailable(str(e)) from e
    data = resp.json()["data"]
    vecs = [np.asarray(item["embedding"], dtype=np.float32) for item in sorted(data, key=lambda x: x["index"])]
    arr = np.stack(vecs, axis=0)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return arr / norms


def warmup() -> None:
    """启动时跑一次,避免首请求冷启。失败不阻塞。"""
    try:
        arr = _post_embeddings(["warmup"])
        log.info("embedding warmup ok, shape=%s", arr.shape)
    except EmbeddingUnavailable as e:
        log.warning("embedding warmup 失败,后续请求将降级: %s", e)


def encode_query(text: str) -> np.ndarray:
    return _post_embeddings([BGE_QUERY_PREFIX + text])[0]


def encode_doc(text: str) -> np.ndarray:
    return _post_embeddings([text])[0]


def encode_docs(texts: Sequence[str]) -> np.ndarray:
    return _post_embeddings(list(texts))


def embedding_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
