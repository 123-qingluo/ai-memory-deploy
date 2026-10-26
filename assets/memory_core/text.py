"""文本归一化、向量数学、嵌入与 LLM 客户端。

零强依赖：numpy 存在时用于批量余弦加速，缺失时退回纯 Python。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from array import array
from typing import Iterable, Sequence

try:  # 可选加速
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover - 环境差异
    _np = None


# ---------------------------------------------------------------- text utils

def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", text)


def char_ngrams(text: str, n: int) -> list[str]:
    text = normalize_text(text)
    if n <= 0 or len(text) < n:
        return []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def ngram_set(text: str, n: int = 2, min_len: int = 1) -> set[str]:
    grams = char_ngrams(text, n)
    if grams:
        return set(grams)
    norm = normalize_text(text)
    return set(norm) if len(norm) >= min_len else set()


def lexical_overlap(query: str, text: str) -> float:
    """查询词覆盖度：query 的字符二元组有多大比例出现在 text 里。"""
    q = ngram_set(query, 2)
    if not q:
        return 0.0
    t = ngram_set(text, 2)
    if not t:
        return 0.0
    return len(q & t) / len(q)


def title_jaccard(a: str, b: str) -> float:
    sa, sb = ngram_set(a, 2), ngram_set(b, 2)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------- vector math

def normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return [0.0 for _ in vec]
    return [x / norm for x in vec]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)) / (na * nb))


def cosine_scores(query: Sequence[float], vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        return []
    if _np is not None:
        try:
            q = _np.asarray(query, dtype="float32")
            m = _np.asarray(vectors, dtype="float32")
            qn = _np.linalg.norm(q) or 1.0
            mn = _np.linalg.norm(m, axis=1)
            mn[mn == 0] = 1.0
            return (m @ q / (mn * qn)).tolist()
        except Exception:  # pragma: no cover
            pass
    return [cosine(query, v) for v in vectors]


def novelty_energy(vec: Sequence[float], basis: Iterable[Sequence[float]]) -> float:
    """新向量落在旧向量张成空间之外的能量占比（0~1）。"""
    v = normalize(vec)
    ortho: list[list[float]] = []
    for raw in list(basis)[:8]:
        w = list(raw)
        for u in ortho:
            d = dot(w, u)
            w = [wi - d * ui for wi, ui in zip(w, u)]
        n = math.sqrt(sum(x * x for x in w))
        if n > 1e-6:
            ortho.append([x / n for x in w])
    if not ortho:
        return 1.0
    proj_sq = 0.0
    for u in ortho:
        d = dot(v, u)
        proj_sq += d * d
    return max(0.0, min(1.0, 1.0 - proj_sq))


def vec_to_blob(vec: Sequence[float]) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


def blob_to_vec(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


# ---------------------------------------------------------------- embedders

class HashingEmbedder:
    """确定性字符 n-gram 特征哈希。

    只用于离线自检与应急降级：它是词面相似度，不是语义模型，
    不能替代生产环境的 bge 一类嵌入。阈值也按词面分布单独配置。
    """

    kind = "hashing"

    def __init__(self, dim: int = 512):
        self.dim = max(64, int(dim))
        self.model = f"hashing-{self.dim}"

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = normalize_text(text)
        for n, weight in ((1, 0.40), (2, 1.00), (3, 0.75)):
            for gram in char_ngrams(norm, n):
                digest = hashlib.md5(gram.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign * weight
        return normalize(vec)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class HttpEmbedder:
    """OpenAI 兼容 /embeddings。失败直接抛错，由调用方降级并留 trace。"""

    kind = "http"

    def __init__(self, base_url: str, model: str, api_key: str = "", dim: int = 0, timeout_s: float = 15):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dim = int(dim or 0)
        self.timeout_s = float(timeout_s)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [normalize(item["embedding"]) for item in items]
        if self.dim and vectors and len(vectors[0]) != self.dim:
            # 维度漂移时以服务端为准，但把事实写进异常，避免静默错配
            raise ValueError(f"embedding dim mismatch: config={self.dim} server={len(vectors[0])}")
        return vectors


def build_embedder(cfg: dict):
    emb = cfg.get("embedding", {})
    provider = (emb.get("provider") or "hashing").lower()
    if provider == "http":
        return HttpEmbedder(
            base_url=emb.get("base_url", ""),
            model=emb.get("model", ""),
            api_key=emb.get("api_key", ""),
            dim=int(emb.get("dim", 0) or 0),
            timeout_s=float(emb.get("timeout_s", 15)),
        )
    return HashingEmbedder(dim=int(emb.get("dim", 512) or 512))


# ---------------------------------------------------------------- LLM

class NoneLLM:
    available = False
    model = "none"

    def complete(self, system: str, user: str) -> str:
        raise RuntimeError("LLM 未配置")


class HttpLLM:
    """OpenAI 兼容 /chat/completions。失败不重试：宁可放弃本轮，避免雪崩。"""

    available = True

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout_s: float = 30):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = float(timeout_s)

    def complete(self, system: str, user: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def build_llm(cfg: dict):
    llm = cfg.get("llm", {})
    if (llm.get("provider") or "none").lower() != "http":
        return NoneLLM()
    return HttpLLM(
        base_url=llm.get("base_url", ""),
        model=llm.get("model", ""),
        api_key=llm.get("api_key", ""),
        timeout_s=float(llm.get("timeout_s", 30)),
    )


def parse_json_array(text: str):
    """从模型输出里抠出第一个 JSON 数组；失败返回 None（调用方放弃本轮）。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None
