import math
import re
from collections import defaultdict


EMBEDDING_DIMENSIONS = 128


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())


def embed_text(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in tokenize(text):
        index = hash_token(token) % EMBEDDING_DIMENSIONS
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def hash_token(token: str) -> int:
    value = 2166136261
    for byte in token.encode("utf-8"):
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def keyword_score(query: str, text: str) -> float:
    terms = tokenize(query)
    if not terms:
        return 0.0
    frequencies: dict[str, int] = defaultdict(int)
    for token in tokenize(text):
        frequencies[token] += 1
    matched = sum(min(frequencies[term], 3) for term in terms)
    return min(matched / len(terms), 1.0)


def chunk_text(text: str, title: str, max_chars: int = 1200) -> list[dict[str, str | int]]:
    chunks: list[dict[str, str | int]] = []
    heading = title
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        content = "\n\n".join(buffer).strip()
        if content:
            chunks.append({"content": content, "heading": heading, "position": len(chunks)})
        buffer = []
        size = 0

    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if paragraph.startswith("#"):
            flush()
            heading = paragraph.lstrip("# ").strip() or title
        if size and size + len(paragraph) > max_chars:
            flush()
        if len(paragraph) > max_chars:
            for start in range(0, len(paragraph), max_chars):
                part = paragraph[start : start + max_chars]
                chunks.append({"content": part, "heading": heading, "position": len(chunks)})
            continue
        buffer.append(paragraph)
        size += len(paragraph)
    flush()
    return chunks
